#!/bin/sh

for i in `seq 1 60`
do
    if [ -d /run/librarypi ]; then break; fi
    sleep 1
done

$(dirname $0)/main.py
