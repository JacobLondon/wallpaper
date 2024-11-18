#!/bin/bash

rm *.exe
gcc host.c -o WallpaperHost
gcc send.c -o WallpaperSend
