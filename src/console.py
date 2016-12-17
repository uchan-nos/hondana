import sys


def log(msg):
    '''log outputs msg to stderr and flushes stderr.'''
    print >>sys.stderr, msg
    sys.stderr.flush()
