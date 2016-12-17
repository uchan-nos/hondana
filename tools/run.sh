#!/bin/bash

for i in `seq 1 60`
do
    if [ -d /run/hondana ]; then break; fi
    sleep 1
done

SCRIPT_DIR=$(cd $(dirname ${BASH_SOURCE[0]}) && pwd)
${SCRIPT_DIR}/../src/main.py
