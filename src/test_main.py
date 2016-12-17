# vim: set encoding=utf-8
from collections import defaultdict
from datetime import datetime
import jinja2
import json
import mock
import os
import shutil
import tempfile

from main import (
        LineReader, ThreadLineReader, Messages,
        EpiphanyBrowser, BrowserReturnPositioner, EmployeeIDScanner,
        Kintone, KintoneLogger, MessagePrinter, Sound, BookProcedure)


def test_LineReader():
    reader = LineReader()
    reader.append('foo')
    assert reader.readline() is None

    reader.append('\nbar\nfiz\n')
    assert reader.readline() == 'foo'
    assert reader.readline() == 'bar'

    reader.skiplines()
    assert reader.readline() is None


def test_ThreadLineReader():
    def sync_write(data):
        reader.clear_processed()
        os.write(wp, data)
        reader.wait_processed()

    rp, wp = os.pipe()
    reader = ThreadLineReader(rp)
    reader.start()
    assert reader.readline() is None

    sync_write('foo\n')
    assert reader.readline() is None

    reader.set_next_flag()
    sync_write('bar\n')
    assert reader.readline() == 'bar'

    reader.terminate()


def test_ThreadLineReader_must_stop():
    rp, wp = os.pipe()
    reader = ThreadLineReader(rp)
    reader.start()
    reader.set_next_flag()

    begin_dt = datetime.utcnow()
    reader.readline(timeout=1)
    end_dt = datetime.utcnow()

    assert (end_dt - begin_dt).total_seconds() > 0.9

    reader.terminate()


def test_BrowserReturnPositioner():
    fake_browser = mock.create_autospec(EpiphanyBrowser)
    html_template = jinja2.Template('genre: {{ genre }}')
    tempdir = tempfile.mkdtemp()
    try:
        positioner = BrowserReturnPositioner(fake_browser, html_template, tempdir)
        positioner.show('infraA[棚3]')

        assert fake_browser.open.called

        positioner.hide()

        assert fake_browser.close.called
    finally:
        shutil.rmtree(tempdir)


def test_KintoneLogger():
    kintone = mock.create_autospec(Kintone)
    logger = KintoneLogger('system1', kintone)
    logger.log_nfc_connected('0123', 'user-hoge')

    assert kintone.add_log.called

    system_id, json_msg = kintone.add_log.call_args[0]
    json_obj = json.loads(json_msg)

    assert system_id == 'system1'
    assert json_obj['employee_id'] == '0123'
    assert json_obj['user_code'] == 'user-hoge'


def create_book_procedure(id_user_map, book_records):
    class FakeKintone(object):
        def __init__(self):
            self.called_map = defaultdict(int)

        def fetch_user_code(self, employee_id):
            self.called_map['fetch_user_code'] += 1
            return id_user_map[employee_id]

        def find_book_records(self, barcode):
            self.called_map['find_book_records'] += 1
            return [r for r in book_records
                    if r[u'isbn'][u'value'] == barcode]

        def borrow_book(self, book_record, user_code):
            self.called_map['borrow_book'] += 1
            if book_record[u'STATUS'][u'value'] == u'本棚にあります':
                return True # success
            return False

        def return_book(self, book_record, user_code):
            self.called_map['return_book'] += 1
            if book_record[u'STATUS'][u'value'] == u'レンタル中':
                return True # success
            return False

        def add_log(self, system_id, json_msg):
            self.called_map['add_log'] += 1
            self._system_id = system_id
            self._json_msg = json_msg

    msg_printer = mock.create_autospec(MessagePrinter)
    kintone = FakeKintone()
    logger = mock.create_autospec(KintoneLogger)
    positioner = mock.create_autospec(BrowserReturnPositioner)
    id_scanner = mock.create_autospec(EmployeeIDScanner)
    sound = mock.create_autospec(Sound)
    line_reader = mock.create_autospec(ThreadLineReader)

    return {
        'procedure': BookProcedure(
            msg_printer, kintone, logger, positioner,
            id_scanner, sound, line_reader),
        'msg_printer': msg_printer,
        'kintone': kintone,
        'logger': logger,
        'positioner': positioner,
        'id_scanner': id_scanner,
        'sound': sound,
        'line_reader': line_reader
    }


def create_single_line_field(value):
    return {u'type': 'SINGLE_LINE_TEXT', u'value': value}


def create_drop_down_field(value):
    return {u'type': 'DROP_DOWN', u'value': value}


def create_user_select_field(user_codes):
    return {u'type': 'USER_SELECT',
            u'value': [{u'code': c, u'name': u'佐藤'} for c in user_codes]}


def create_assignee_field(user_codes):
    return {u'type': 'STATUS_ASSIGNEE',
            u'value': [{u'code': c, u'name': u'佐藤'} for c in user_codes]}


def create_status_field(value):
    return {u'type': 'STATUS', u'value': value}


def create_book_record(isbn, genre, borrowed_by):
    record = {
        u'isbn': create_single_line_field(isbn.decode('utf-8')),
        u'type': create_drop_down_field(genre.decode('utf-8')),
    }

    if borrowed_by is None:
        record[u'STATUS'] = create_status_field(u'本棚にあります')
        record[u'STATUS_ASSIGNEE'] = create_assignee_field([])
    else:
        record[u'STATUS'] = create_status_field(u'レンタル中')
        record[u'STATUS_ASSIGNEE'] = create_assignee_field(
            [borrowed_by.decode('utf-8')])

    return record


def test_BookProcedure_borrow():
    o = create_book_procedure(
        {
            '0123': 'hoge-user'
        },
        [
            create_book_record('9784789838078', 'PGその他[棚6]', None),
        ])
    
    o['id_scanner'].scan.return_value = '0123'
    o['line_reader'].readline.return_value = '9784789838078'

    o['procedure'].process_once()

    assert o['line_reader'].readline.called
    assert o['kintone'].called_map['borrow_book'] == 1
    assert o['sound'].play_se.called
    assert not o['positioner'].show.called

    o['logger'].log_completed.assert_called_once_with(
        'hoge-user', '9784789838078', 'successfully borrowed a book')


def test_BookProcedure_return():
    o = create_book_procedure(
        {
            '0123': 'hoge-user'
        },
        [
            create_book_record('9784789838078', 'PGその他[棚6]', 'hoge-user'),
        ])
    
    o['id_scanner'].scan.return_value = '0123'
    o['line_reader'].readline.return_value = '9784789838078'

    o['procedure'].process_once()

    assert o['kintone'].called_map['return_book'] == 1
    o['positioner'].show.assert_called_once_with('PGその他[棚6]')

    o['logger'].log_completed.assert_called_once_with(
        'hoge-user', '9784789838078', 'successfully returned a book')


def test_BookProcedure_no_free_book():
    o = create_book_procedure(
        {
            '0123': 'hoge-user'
        },
        [
            create_book_record('9784789838078', 'PGその他[棚6]', 'foo user'),
        ])
    
    o['id_scanner'].scan.return_value = '0123'
    o['line_reader'].readline.return_value = '9784789838078'

    o['procedure'].process_once()

    assert o['kintone'].called_map['return_book'] == 0
    assert o['kintone'].called_map['borrow_book'] == 0

    o['logger'].log_completed.assert_called_once_with(
        'hoge-user', '9784789838078', 'book has already been borrowed')


def test_BookProcedure_unknown_user():
    o = create_book_procedure(
        {
            '0123': 'hoge-user'
        },
        [
            create_book_record('9784789838078', 'PGその他[棚6]', 'foo user'),
        ])
    
    o['id_scanner'].scan.return_value = '4567' # unknown employee
    o['line_reader'].readline.return_value = '9784789838078'

    o['procedure'].process_once()

    o['msg_printer'].put.assert_called_with(Messages.FAILED_TO_FETCH_USERCODE)

    assert o['kintone'].called_map['find_book_records'] == 0
    assert not o['logger'].log_completed.called
