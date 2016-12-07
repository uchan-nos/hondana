#!/usr/bin/python
# vim: set fileencoding=utf-8

from collections import namedtuple
from datetime import datetime
import yaml

import pykintone
import pykintone.model_result as mr


KintoneEnv = namedtuple('KintoneEnv',
    ['kintone', 'meibo_app_id', 'book_app_id', 'log_app_id'])


API_USER = 'Administrator'
SYSTEM_USER = 'kota-uchida'


def init():
    kin = pykintone.load('kintone.yml')

    with open('kintone.yml') as f:
        apps = yaml.load(f)['apps']

    def get_id(app_name):
        for name, value in apps.iteritems():
            if name == app_name:
                return value['id']
        raise SystemError('no such app: ' + app_name)

    return KintoneEnv(
        kintone=kin,
        meibo_app_id=get_id('meibo'),
        book_app_id=get_id('book'),
        log_app_id=get_id('log'))


def fetch_user_code(env, employee_id):
    app = env.kintone.app(env.meibo_app_id)
    res = app.select('employeeNumber = "{}"'.format(employee_id))
    if not res.ok:
        raise RuntimeError(res.error)
    if len(res.records) == 0:
        raise RuntimeError('no such employee id: {}'.format(employee_id))

    #user_select_value = res.records[0][u'ユーザー'][u'value']
    #if len(user_select_value) != 1:
    #    raise RuntimeError('invalid meibo value: {}'.format(user_select_value))

    #return user_select_value[0][u'code']

    user_code = res.records[0][u'code'][u'value'].strip()
    if user_code == '':
        raise RuntimeError('blank user code for employee {}'.format(employee_id))
    return user_code


def find_book_records(env, isbn):
    if len(isbn) == 10:
        isbn_len = 10
        query = 'isbn = "{}"'
    elif len(isbn) == 13:
        isbn_len = 13
        query = 'isbn13 = "{}"'
    else:
        raise RuntimeError('invalid ISBN length: {}'.format(isbn))

    book_app = env.kintone.app(env.book_app_id)
    res = book_app.select(query.format(isbn))
    if not res.ok:
        raise RuntimeError(res.error)

    return res.records


def find_field_by_type(record, field_type):
    for k, v in record.iteritems():
        if v[u'type'] == field_type:
            return v
    return None


def book_is_borrowed(record, user_code):
    assignee = find_field_by_type(record, u'STATUS_ASSIGNEE')
    if assignee is None:
        return False

    for v in assignee[u'value']:
        if v[u'code'] == user_code:
            return get_record_status(record) == u'レンタル中'
    return False


def book_is_free(record):
    return get_record_status(record) == u'本棚にあります'

def find_first(records, pred):
    for r in records:
        if pred(r):
            return r
    return None

def get_borrowing_users(records):
    users = []
    for r in records:
        assignees = find_field_by_type(r, u'STATUS_ASSIGNEE')[u'value']
        if assignees is None:
            continue
        users.extend(get_user_name(a) for a in assignees)
    return users

def get_user_name(assignee):
    return assignee[u'name'].encode('utf-8').replace(' ', '').replace('　', '')
        
def get_record_status(record):
    status = find_field_by_type(record, u'STATUS')
    if status is None:
        return None

    return status[u'value']


def borrow_book(env, book_record, user_code):
    book_app = env.kintone.app(env.book_app_id)
    if book_is_free(book_record):
        res = book_app.proceed(book_record, u'system_borrow', SYSTEM_USER)
        if not res.ok:
            raise RuntimeError(res.error)
        res = book_app.get(book_record['$id']['value'])
        if not res.ok:
            raise RuntimeError(res.error)
        book_record = res.record
        res = set_assignee(env, book_record, [user_code])
        if not res.ok:
            raise RuntimeError(res.error)
        return True
    return False


def return_book(env, book_record, user_code):
    book_app = env.kintone.app(env.book_app_id)
    if book_is_borrowed(book_record, user_code):
        res = set_assignee(env, book_record, [])
        if not res.ok:
            raise RuntimeError(res.error)
        res = book_app.get(book_record['$id']['value'])
        if not res.ok:
            raise RuntimeError(res.error)
        book_record = res.record
        res = book_app.proceed(book_record, u'返す')
        if not res.ok:
            raise RuntimeError(res.error)
        return True
    return False


def set_assignee(env, book_record, user_codes):
    book_app = env.kintone.app(env.book_app_id)
    url = book_app.API_ROOT.format(
        book_app.account.domain, "record/assignees.json")
    record_id = int(book_record["$id"]["value"])
    record_revision = int(book_record["$revision"]["value"])
    params = {
        "app": book_app.app_id,
        "id": record_id,
        "assignees": user_codes,
        "revision": record_revision
    }
    resp = book_app._request("PUT", url, params_or_data=params)
    r = mr.UpdateResult(resp)
    return r


def add_log(env, system_id, msg, logged_at=None):
    if logged_at is None:
        logged_at = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

    log_app = env.kintone.app(env.log_app_id)
    res = log_app.create({
        'logged_at': {'value': logged_at},
        'system_id': {'value': system_id},
        'message': {'value': msg}
    })

    if not res.ok:
        raise RuntimeError(res.error)
