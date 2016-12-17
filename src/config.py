#!/usr/bin/python

import argparse
import sys
import yaml

from console import log


def fetch_value_from_file(key_str, yaml_file_path):
    '''fetches a value corresponding the given key.'''
    with open(yaml_file_path) as f:
        y = yaml.load(f)

    return fetch_value(key_str, y)


def fetch_value(key_str, dict_obj):
    '''fetches a value corresponding the given key.
    
    >>> fetch_value("foo", {})
    Traceback (most recent call last):
        ...
    KeyError: 'foo'

    >>> fetch_value("foo.bar", {'foo': {'bar': 42}})
    42
    '''
    key_list = key_str.split('.')
    o = dict_obj
    for k in key_list:
        o = o[k]
    return o


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('key', help='key name to be fetched ("." to fetch all)')
    parser.add_argument('conf', help='path to a config file')

    ns = parser.parse_args()

    try:
        val = fetch_value_from_file(ns.key, ns.conf)
    except KeyError:
        log('no such key: ' + ns.key)
        sys.exit(1)

    if isinstance(val, dict) or isinstance(val, list):
        print yaml.dump(val)
    else:
        print val


if __name__ == '__main__':
    main()
