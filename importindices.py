#!/usr/bin/env python

# Downloads elasticsearch indices from wikimedia dumps and imports them
# to an elasticsearch cluster. This should generally be run within the
# same network as the elasticsearch server. This will download from
# dumps.wikimedia.org and write to the local disk. For the largest
# indices more than 30GB of disk space on the import runner will be
# required.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
# http://www.gnu.org/copyleft/gpl.html

import argparse
import datetime
import os
import sys
import tempfile
import urllib2


def last_dump():
    today = datetime.date.today()
    # dumps are on Monday, but run through most of Tuesday. Only take this weeks dumps
    # if it is at least Wednesday. TODO: is this UTC? dumps run from around 16:00 UTC
    # on Monday to 7:00 UTC Wednesday
    if today.weekday() < 3:
        last_dump_delta = datetime.timedelta(days=7+today.weekday())
    else:
        last_dump_delta = datetime.timedelta(days=today.weekday())
    last_dump = today - last_dump_delta
    return last_dump.strftime("%Y%m%d")


def check_index_exists(dest_host, wiki, index_type):
    url = "http://%s:9200/%s_%s" % (dest_host, wiki, index_type)
    print("Check existence:", url)
    request = urllib2.Request(url)
    request.get_method = lambda: 'HEAD'
    # This will throw an error on 404 if the index doesn't exist
    urllib2.urlopen(request)


def main():
    parser = argparse.ArgumentParser(description='import wikimedia elasticsearch dumps',
                                     prog=sys.argv[0])
    parser.add_argument('--dest', dest="dest", default='estest1001',
                        help='server to import indices into')
    parser.add_argument('--type', dest='type', default='content',
                        help='type of index to import, either content or general')
    parser.add_argument('--date', dest='date', default=last_dump(),
                        help='date to load dump from')
    parser.add_argument('--temp-dir', dest='temp_dir', default=None,
                        help='directory to download index into')
    parser.add_argument('wikis', nargs='+', help='list of wikis to import')
    args = parser.parse_args()

    for wiki in args.wikis:
        src_url = 'http://dumps.wikimedia.org/other/cirrussearch/%s/%s-%s-cirrussearch-%s.json.gz' % \
            (args.date, wiki, args.date, args.type)
        fd, temp_path = tempfile.mkstemp(dir=args.temp_dir)
        print("Downloading ", src_url, " to ", temp_path)
        os.system("curl -o %s %s" % (temp_path, src_url))
        check_index_exists(args.dest, wiki, args.type)
        dest_url = "http://%s:9200/%s_%s/_bulk" % (args.dest, wiki, args.type)
        cmd = 'curl -s %s --data-binary @- > /dev/null' % (dest_url)
        os.system("pv %s | zcat | parallel --pipe -L 100 -j3 '%s'" % (temp_path, cmd))
        os.close(fd)
        os.remove(temp_path)

if __name__ == "__main__":
    main()
