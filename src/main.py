#!/usr/bin/python
# vim: set encoding=utf-8
from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
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

log = logging.getLogger(__name__)
queue = Queue.Queue()
waiting_line = threading.Event()
genre_page_pipe = None
genre_page_name = None
terminating = False
jinja_env = jinja2.Environment(loader=jinja2.FileSystemLoader('.'))
kintone_env = kintone.init()
scanned_employee_id = None
system_id = None


class LineReader:
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


# read_lines will be run on another thread
def read_lines(queue, quit_pipe):
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN | select.POLLPRI | select.POLLHUP)
    poll.register(quit_pipe, select.POLLHUP)

    reader = LineReader()
    def process_line(readbytes):
        reader.append(readbytes)
        line = reader.readline()
        if line is None:
            return

        if waiting_line.is_set():
            queue.put(line)
            waiting_line.clear()
        reader.skiplines()

    while not terminating:
        events = poll.poll()
        if not events:
            continue

        for e in events:
            fd, ev = e[0], e[1]
            if fd == quit_pipe and (ev & select.POLLHUP) != 0:
                # quit
                return
            elif fd == 0 and (ev & (select.POLLIN | select.POLLPRI)) != 0:
                # there are some data
                readbytes = os.read(fd, 1024)
                if not readbytes:
                    return
                process_line(readbytes)
            elif fd == 0 and (ev & select.POLLHUP) != 0:
                # stdin closed
                return
            else:
                print_flush('Unexpected event: fd={}, ev={}'.format(fd, ev))


def dequeue_one(queue, timeout=None):
    begin_dt = datetime.utcnow()
    while not terminating:
        if queue.empty():
            if timeout is not None and \
                    (datetime.utcnow() - begin_dt) > timeout:
                return None

            time.sleep(0.2)
            continue
        return queue.get()
    sys.exit()


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

def show_genre_page(genre):
    global genre_page_pipe, genre_page_name
    close_genre_page()
    genre_class = get_genre_class(genre)
    t = jinja_env.get_template('hondana.html')
    html = t.render(genre=genre_class)
    with tempfile.NamedTemporaryFile(dir=TEMPDIR, delete=False) as f:
        genre_page_name = f.name
        f.write(html.encode('utf-8'))

    cmd = ['epiphany', '-a', '--profile', '/home/pi/.epiconfig', genre_page_name]
    #cmd = ['./open_browser.sh', genre_page_name]
    genre_page_pipe = Popen(cmd, close_fds=True, stderr=DEVNULL)


def close_genre_page():
    global genre_page_pipe

    if genre_page_pipe != None:
        genre_page_pipe.terminate()
        genre_page_pipe.wait()
        genre_page_pipe = None

        if os.path.exists(genre_page_name):
            os.remove(genre_page_name)


def on_connect(tag):
    idm, pmm = tag.polling(system_code=0xfe00)
    tag.idm, tag.pmm, tag.sys = idm, pmm, 0xfe00

    global scanned_employee_id
    scanned_employee_id = fetch_employee_id(tag)
    print_flush(scanned_employee_id)
    return


def post_connect():
    employee_id = scanned_employee_id

    close_genre_page()

    cmd = ['aplay', '/home/pi/Downloads/nc75064.wav']
    check_call(cmd, stderr=DEVNULL)
    
    kintone.add_log(kintone_env, system_id, json.dumps({
        'employee_id': str(employee_id),
        'message': 'nfc connected'
    }))


    waiting_line.set()
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

        line = dequeue_one(queue, timedelta(seconds=20))
        if line is None:
            print_flush('Timed out')
            speech('timeout')
            return

        bar_code = line.strip()

        if bar_code.startswith('97'):
            break
        waiting_line.set()
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
    global system_id
    with open('system_id') as f:
        system_id = f.read().strip()

    def sig_handler(signum, frame):
        global terminating
        print_flush('signal handler: ' + str(signum))
        if signum in {signal.SIGINT, signal.SIGTERM}:
            terminating = True

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    quit_pipe, quit_pipe_write = os.pipe()

    th = threading.Thread(target=read_lines, args=(queue, quit_pipe))
    th.start()

    with nfc.ContactlessFrontend('usb') as clf:
        rdwr = {
            'on-connect': on_connect
        }
        while not terminating:
            print 'Touch IC card'
            try:
                clf.connect(rdwr=rdwr, terminate=lambda: terminating)
            except:
                print_flush('Connect error')
                continue
            if terminating:
                break

            try:
                post_connect()
            except requests.exceptions.ConnectionError as e:
                print_flush('Network error: {}'.format(str(e)))

    # send quit signal to a thread
    os.close(quit_pipe_write)
    
    th.join()


if __name__ == '__main__':
    main()
