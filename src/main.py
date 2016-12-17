#!/usr/bin/python
# vim: set encoding=utf-8
from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
from collections import namedtuple
from datetime import datetime, timedelta
import jinja2
import json
import logging
import nfc
import os
import Queue
import select
import signal
import sys
from subprocess import Popen, check_call
import tempfile
import threading
import time
import re
from urlparse import urlparse
import requests

import kintone


TEMPDIR = '/run/librarypi'
CMD_BORROW = '2000000000008'
CMD_RETURN = '1000000000009'
DEVNULL = open('/dev/null', 'w')
DICTIONARY = {'経営':'management', '仕様':'specification', 'Windows': 'windows', 'Web技術':'web', '技術書':'tech_book', 'アルゴリズム':'algorithm', '設計': 'design', 'コーディング': 'coding', 'プロジェクト':'project', '低レイヤ':'low_layer', '言語':'language', 'その他言語':'others', 'テスト':'test', 'C/C++':'c_cpp', '寄贈本':'donation', '雑誌':'magazine', '辞典':'dictionary'}

terminating = False


class LineReader(object):
    def __init__(self):
        self._pool = ''

    def append(self, dat):
        self._pool += dat

    def readline(self):
        '''Reads until a LF character and returns it without a LF character.'''
        lf_pos = self._pool.find('\n')
        if lf_pos == -1:
            return None
        line = self._pool[:lf_pos]
        self._pool  = self._pool[lf_pos+1:]
        return line

    def skiplines(self):
        lf_pos = self._pool.rfind('\n')
        if lf_pos == -1:
            return
        self._pool = self._pool[lf_pos+1:]


class ThreadLineReader(threading.Thread):
    def __init__(self, read_fd):
        super(ThreadLineReader, self).__init__()
        self._read_fd = read_fd
        self._line_reader = LineReader()
        self._next_flag = threading.Event()
        self._quit_pipe, self._quit_pipe_write = os.pipe()
        self._lines = Queue.Queue()

        # for testing
        self._processed = threading.Event()

    def run(self):
        poll = select.poll()
        poll.register(self._read_fd, select.POLLIN | select.POLLPRI | select.POLLHUP)
        poll.register(self._quit_pipe, select.POLLHUP)

        reader = LineReader()
        def process_line(readbytes):
            reader.append(readbytes)
            line = reader.readline()
            if line is None:
                return

            if self._next_flag.is_set():
                self._lines.put(line)
                self._next_flag.clear()
            reader.skiplines()

            self._processed.set()

        while True:
            events = poll.poll()
            if not events:
                continue

            for e in events:
                fd, ev = e[0], e[1]
                if fd == self._quit_pipe and (ev & select.POLLHUP) != 0:
                    # quit
                    return
                elif fd == self._read_fd and (ev & (select.POLLIN | select.POLLPRI)) != 0:
                    # there are some data
                    readbytes = os.read(fd, 1024)
                    if not readbytes:
                        # EOF
                        return
                    process_line(readbytes)
                elif fd == self._read_fd and (ev & select.POLLHUP) != 0:
                    # read_fd closed
                    return
                else:
                    self.log(msg)

    def set_next_flag(self):
        '''set_next_flag sets the next flag, which requests this reader
        to read a line inputted after calling this method.'''
        self._next_flag.set()

    def readline(self, timeout=None):
        '''readline reads a line from read_fd.

        If the next flag is not set, readline returns immediately
        the last inputted value or None.
        Otherwise, readline waits until at least one line comes and returns
        the inputted value. If it timed out, then returns None.
        '''
        if not self._next_flag.is_set():
            try:
                return self._lines.get(block=False)
            except Queue.Empty:
                return None

        try:
            return self._lines.get(block=True, timeout=timeout)
        except Queue.Empty:
            return None

    def terminate(self):
        '''terminate requests this thread to be stopped.'''
        os.close(self._quit_pipe_write)

    def log(self, msg):
        '''log logs the given message. User can override this method.'''
        log(msg)

    def clear_processed(self):
        # for testing
        self._processed.clear()

    def wait_processed(self, timeout=None):
        # for testing
        if self._processed.wait(timeout):
            self._processed.clear()
            return True
        return False


def print_flush(msg):
    print msg
    sys.stdout.flush()


def speech(msg):
    cmd = ['./speech.sh', msg]
    check_call(cmd, stderr=DEVNULL)


def fetch_employee_id(tag):
    sc = nfc.tag.tt3.ServiceCode(93, 0x0b)
    bc = nfc.tag.tt3.BlockCode(1, service=0)
    data = tag.read_without_encryption([sc], [bc])
    if data[6:9] == 'CBZ':
        return data[10:]

    log.warning('No employee id: %s', data)
    return None


def get_genre_class(genre):
    genre = re.sub(r'\[.*\]', '', genre)
    if genre in DICTIONARY:
        return DICTIONARY[genre]
    else:
        return genre.lower()


class Messages(object):
    MessagePair = namedtuple('MessagePair', ['log', 'speech'])

    EMPLOYEE_ID_SCANNED = MessagePair(
        'scanned employee id: {id}',
        None)
    FAILED_TO_SCAN_EMPLOYEE_ID = MessagePair(
        'failed to scan employee id',
        '社員番号の読み取りに失敗しました。もう一度タッチしてください。')
    FAILED_TO_FETCH_USERCODE = MessagePair(
        'failed to fetch user code',
        '社員番号を確認できませんでした。図書委員まで連絡してください。')
    PLEASE_SCAN_BARCODE = MessagePair(
        'please a barcode of your book',
        'きゅうななで始まるバーコードをスキャンしてください')
    TIMED_OUT = MessagePair(
        'timed out',
        'timeout')
    BARCODE_IS_NOT_ISBN = MessagePair(
        'Barcode is not an ISBN',
        'そっちじゃなぁぁぁい！違うほうのバーコードをスキャンしてください')
    ALREADY_BORROWED = MessagePair(
        'this book has already been borrowed by {names}',
        '{names}がすでに借りています')
    NOT_REGISTERED = MessagePair(
        'this book is not registered (please contact to librarians',
        'この本は未登録のようです。図書委員まで連絡してください。')
    BOOK_BORROWED = MessagePair(
        'successfully borrowed a book',
        'かしだし手続きが完了しました')
    BOOK_RETURNED = MessagePair(
        'successfully returned a book',
        '返却手続きが完了しました')
    KINTONE_ERROR = MessagePair(
        'kintone returned an error',
        'キントーンがエラーを返しました')


class MessagePrinter(object):
    def put(self, msg_pair, **kwargs):
        if msg_pair.log:
            print_flush(msg_pair.log.format(**kwargs))
        if msg_pair.speech:
            speech(msg_pair.speech.format(**kwargs))


class EpiphanyBrowser(object):
    def __init__(self):
        self._process = None

    def open(self, url):
        cmd = ['epiphany', '-a', '--profile', '/home/pi/.epiconfig',
                self._temporary_html_path]
        self._process = Popen(cmd, close_fds=True, stderr=DEVNULL)

    def close(self):
        if self._process is None:
            return
        self._process.terminate()
        self._process.wait()
        self._process = None


class BrowserReturnPositioner(object):
    def __init__(self, browser, html_template, temporary_dir_path):
        self._browser = browser
        self._html_template = html_template
        self._temporary_html_path = None
        self._temporary_dir_path = temporary_dir_path

    def show(self, genre_name):
        self.hide()
        genre_class = get_genre_class(genre_name)
        html = self._html_template.render(genre=genre_class)
        with tempfile.NamedTemporaryFile(
                dir=self._temporary_dir_path, delete=False) as f:
            self._temporary_html_path = f.name
            f.write(html.encode('utf-8'))
        self._browser.open(self._temporary_html_path)

    def hide(self):
        self._browser.close()


class EmployeeIDScanner(object):
    def __init__(self, clf):
        self._clf = clf
        self._scanned_id = None
        self._request_terminate = False

    def _on_connect(self, tag):
        idm, pmm = tag.polling(system_code=0xfe00)
        tag.idm, tag.pmm, tag.sys = idm, pmm, 0xfe00
        self._scanned_id = fetch_employee_id(tag)

    def terminate(self):
        self._request_terminate = True

    def scan(self):
        '''scan scans one employee id and returns it,
        or None if interrupted by self.terminate().'''
        rdwr = {
            'on-connect': self._on_connect
        }
        self._clf.connect(rdwr=rdwr, terminate=lambda: self._request_terminate)
        if self._request_terminate:
            return None
        return self._scanned_id


class Kintone(object):
    def __init__(self, kintone_env):
        this._env = kintone_env

    def fetch_user_code(self, employee_id):
        return kintone.fetch_user_code(self._env, employee_id)

    def find_book_records(self, barcode):
        return kintone.find_book_records(self._env, barcode)

    def borrow_book(self, book_record, user_code):
        return kintone.borrow_book(self._env, book_record, user_code)

    def return_book(self, book_record, user_code):
        return kintone.return_book(self._env, book_record, user_code)

    def add_log(self, system_id, json_msg):
        kintone.add_log(self._env, system_id, json_msg)


class KintoneLogger(object):
    def __init__(self, system_id, kintone):
        self._system_id = system_id
        self._kintone = kintone

    def log_nfc_connected(self, employee_id, user_code):
        self._kintone.add_log(self._system_id, json.dumps({
            'employee_id': str(employee_id),
            'user_code': 'failed to fetch user code' if user_code is None else user_code,
            'message':  'nfc connected'
        }))

    def log_completed(self, user_code, barcode, message):
        self._kintone.add_log(self._system_id, json.dumps({
            'user_code': user_code,
            'book_isbn': barcode,
            'message': message
        }))


class Sound(object):
    def play_se(self):
        cmd = ['aplay', '/home/pi/Downloads/nc75064.wav']
        check_call(cmd, stderr=DEVNULL)


class BookProcedure(object):
    def __init__(self, msg_printer, kintone, logger, positioner,
            id_scanner, sound, line_reader):
        self._msg_printer = msg_printer
        self._kintone = kintone
        self._logger = logger
        self._positioner = positioner
        self._id_scanner = id_scanner
        self._sound = sound
        self._line_reader = line_reader

    def process_once(self):
        employee_id = self.scan_employee_id()
        self._msg_printer.put(Messages.EMPLOYEE_ID_SCANNED, id=employee_id)
        self._sound.play_se()
        self._positioner.hide()

        user_code = None
        try:
            user_code = self._kintone.fetch_user_code(employee_id)
        except:
            pass

        self._logger.log_nfc_connected(employee_id, user_code)
        if user_code is None:
            self._msg_printer.put(Messages.FAILED_TO_FETCH_USERCODE)
            return

        barcode = self.scan_barcode()
        if barcode is None:
            return

        book_records = self._kintone.find_book_records(barcode)
        borrowed_book_record = kintone.find_first(
                book_records,
                lambda r: kintone.book_is_borrowed(r, user_code))

        log_message = None
        if borrowed_book_record is None:
            log_message = self.borrow_book(book_records, user_code)
        else:
            log_message = self.return_book(borrowed_book_record, user_code)

        self._logger.log_completed(user_code, barcode, log_message)

    def scan_employee_id(self):
        while True:
            try:
                return self._id_scanner.scan()
            except:
                self._msg_printer.put(Message.FAILED_TO_SCAN_EMPLOYEE_ID)

    def terminate(self):
        self._id_scanner.terminate()

    def scan_barcode(self):
        self._line_reader.set_next_flag()
        self._msg_printer.put(Messages.PLEASE_SCAN_BARCODE)

        while True:
            line = self._line_reader.readline(timeout=20)
            if line is None:
                self._msg_printer.put(Messages.TIMED_OUT)
                return None
            barcode = line.strip()
            if barcode.startswith('97'):
                return barcode
            self._line_reader.set_next_flag()
            self._msg_printer.put(Messages.BARCODE_IS_NOT_ISBN)

    def borrow_book(self, book_records, user_code):
        free_book_record = kintone.find_first(book_records, kintone.book_is_free)
        if free_book_record is None:
            user_names = kintone.get_borrowing_users(book_records)
            if user_names:
                names = ' '.join(name + 'さん' for name in user_names)
                self._msg_printer.put(Messages.ALREADY_BORROWED, names=names)
                return 'book has already been borrowed'
            else:
                self._msg_printer.put(Messages.NOT_REGISTERED)
                return 'book is not registered'

        if self._kintone.borrow_book(free_book_record, user_code):
            self._msg_printer.put(Messages.BOOK_BORROWED)
            return 'successfully borrowed a book'

        self._msg_printer.put(Messages.KINTONE_ERROR)
        return 'kintone returned an error'

    def return_book(self, book_record, user_code):
        if self._kintone.return_book(book_record, user_code):
            self._msg_printer.put(Messages.BOOK_RETURNED)
            self._positioner.show(book_record[u'type'][u'value'].encode('utf-8'))
            return 'successfully returned a book'

        self._msg_printer.put(Messages.KINTONE_ERROR)
        return 'kintone returned an error'


def post_connect():
    employee_id = scanned_employee_id

    close_genre_page()

    cmd = ['aplay', '/home/pi/Downloads/nc75064.wav']
    check_call(cmd, stderr=DEVNULL)
    
    kintone.add_log(kintone_env, system_id, json.dumps({
        'employee_id': str(employee_id),
        'message': 'nfc connected'
    }))

    #waiting_line.set()
    reader.set_next_flag()
    try:
        user_code = kintone.fetch_user_code(kintone_env, employee_id)
    except:
        print_flush('Failed to fetch user code')
        speech("社員番号を確認できませんでした。図書委員まで連絡してください。")
        return
    print_flush('User code: ' + user_code)
    speech("きゅうななで始まるバーコードをスキャンしてください")
    while True:
        print_flush('Scan barcode')

        #line = dequeue_one(queue, timedelta(seconds=20))
        line = reader.readline(timeout=20)
        if line is None:
            print_flush('Timed out')
            speech('timeout')
            return

        bar_code = line.strip()

        if bar_code.startswith('97'):
            break
        #waiting_line.set()
        reader.set_next_flag()
        print_flush('Barcode is not an ISBN')  
        speech('そっちじゃなぁぁぁい！違うほうのバーコードをスキャンしてください')
        #speech('社員証タッチからやりなおしてください')
        #return

    book_records = kintone.find_book_records(kintone_env, bar_code)
    def borrowed(r):
        return kintone.book_is_borrowed(r, user_code)
    book_record = kintone.find_first(book_records, borrowed)
    if book_record is None:
        book_record = kintone.find_first(book_records, kintone.book_is_free)
        if book_record is None:
            message = 'cannot borrow the book (no free book: someone may be forgetting to change book status)'
            print_flush(message)
            user_names = kintone.get_borrowing_users(book_records)
            if user_names:
                speech('さん '.join(user_names) + 'さんがすでに借りています')
            else:
                # book is not registered
                speech('この本は未登録のようです。図書委員まで連絡してください。')
        elif not kintone.borrow_book(kintone_env, book_record, user_code):
            message = 'cannot borrow the book (kintone error)'
            print_flush(message)
            speech('キントーンがエラーを返しました')
        else:
            message = 'borrow a book'
            print_flush('successful!')
            speech('かしだし手続きが完了しました')
    elif not kintone.return_book(kintone_env, book_record, user_code):
        message = 'cannot return the book (kintone error)'
        print_flush(message)
        speech('キントーンがエラーを返しました')
    else:
        message = 'return a book'
        print_flush('successful!')
        show_genre_page(book_record[u'type'][u'value'].encode('utf-8'))
        speech('返却手続きが完了しました')

    kintone.add_log(kintone_env, system_id, json.dumps({
        'user_code': user_code,
        'book_isbn': bar_code,
        'message': message
    }))


def main():
    if not os.path.exists(TEMPDIR):
        print_flush('Please create temp dir: ' + TEMPDIR)
        sys.exit(1)

    if not os.path.exists('./system_id'):
        print_flush('Please create system_id file')
        sys.exit(1)
    with open('system_id') as f:
        system_id = f.read().strip()

    jinja_env = jinja2.Environment(loader=jinja2.FileSystemLoader('.'))

    line_reader = ThreadLineReader(sys.stdin.fileno())
    line_reader.start()

    kin = Kintone(kintone.init())

    with nfc.ContactlessFrontend('usb') as clf:
        procedure = BookProcedure(
            MessagePrinter(),
            kin,
            KintoneLogger(system_id, kin),
            BrowserReturnPositioner(
                EpiphanyBrowser(),
                jinja_env.get_template('hondana.html'),
                TEMPDIR),
            EmployeeIDScanner(clf),
            Sound(),
            line_reader)

        request_terminate = threading.Event()
        def sig_handler(signum, frame):
            print_flush('signal handler: ' + str(signum))
            if signum in {signal.SIGINT, signal.SIGTERM}:
                request_terminate.set()
                procedure.terminate()

        signal.signal(signal.SIGINT, sig_handler)
        signal.signal(signal.SIGTERM, sig_handler)

        while not request_terminate.is_set():
            procedure.process_once()

    line_reader.terminate()
    line_reader.join()


if __name__ == '__main__':
    main()
