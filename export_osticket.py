#!/usr/bin/env python3
"""
Export osTicket incoming messages (thread_entry.type='M') as .eml files
and bundle them into a tar.gz archive for import on the target mail server.

Modes:
  --test               Build the latest M-mail, print summary + raw to stdout
  --test-id N          Build the mail with thread_entry.id=N, same output
  (default)            Export all M-mails to <outdir>, then create <outdir>.tar.gz

Examples:
  sudo python3 export_osticket_emls.py --test
  sudo python3 export_osticket_emls.py --test-id 6688 --test-out /tmp/test.eml
  sudo python3 export_osticket_emls.py --limit 5 --outdir /tmp/test-export
  sudo python3 export_osticket_emls.py --outdir /root/osticket-eml-export
"""

import argparse
import email
import email.policy
import logging
import os
import sys
import tarfile
import time
from datetime import datetime

try:
    import pymysql
except ImportError:
    sys.exit("Please install: sudo apt-get install python3-pymysql")

# ---------------- CONFIG (adjust this!) ----------------
DB = dict(
    host='localhost',
    user='root',
    password='YOUR_MYSQL_ROOT_PW',
    database='osticket',
    charset='utf8mb4',
)

ATTACH_ROOT = '/var/www/osticket/attachments' # adjust this path accordingly
DEFAULT_OUTDIR = 'osticket-eml-export'
# ----------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-7s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('export')

RELEVANT_HEADERS = (
    'From', 'To', 'Cc', 'Bcc', 'Reply-To',
    'Subject', 'Date', 'Message-ID',
    'In-Reply-To', 'References',
)


# -------------------- DB --------------------

def connect():
    return pymysql.connect(**DB)


def fetch_rows(conn, since=None, limit=None, entry_id=None, order_desc=False):
    sql = """
        SELECT te.id AS entry_id,
               te.created,
               te.format,
               te.body,
               tee.mid,
               tee.headers
        FROM ost_thread_entry te
        JOIN ost_thread_entry_email tee ON tee.thread_entry_id = te.id
        WHERE te.type = 'M'
    """
    params = []
    if since:
        sql += " AND te.created >= %s"
        params.append(since)
    if entry_id:
        sql += " AND te.id = %s"
        params.append(entry_id)
    sql += " ORDER BY te.created " + ("DESC" if order_desc else "ASC")
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    cur = conn.cursor(pymysql.cursors.DictCursor)
    try:
        cur.execute(sql, params)
        for row in cur.fetchall():
            yield row
    finally:
        cur.close()


def read_file(conn, row):
    bk = row['bk']
    if bk == 'F':
        key = row['key']
        path = os.path.join(ATTACH_ROOT, key[0], key)
        with open(path, 'rb') as fh:
            return fh.read()
    if bk == 'D':
        cur = conn.cursor(pymysql.cursors.DictCursor)
        try:
            cur.execute(
                "SELECT filedata FROM ost_file_chunk "
                "WHERE file_id = %s ORDER BY chunk_id",
                (row['file_id'],)
            )
            return b''.join(r['filedata'] for r in cur.fetchall())
        finally:
            cur.close()
    raise ValueError("Unknown Backend bk=%r" % (bk,))


def fetch_attachments(conn, entry_id):
    sql = """
        SELECT a.file_id, a.name AS disp_name, a.inline,
               f.bk, f.key, f.name AS fname, f.type AS mime, f.size
        FROM ost_attachment a
        JOIN ost_file f ON f.id = a.file_id
        WHERE a.type = 'H' AND a.object_id = %s
    """
    out = []
    cur = conn.cursor(pymysql.cursors.DictCursor)
    try:
        cur.execute(sql, (entry_id,))
        rows = cur.fetchall()
    finally:
        cur.close()

    for row in rows:
        try:
            data = read_file(conn, row)
        except Exception as e:
            log.warning("Attachment file_id=%s (%s) nicht lesbar: %s",
                        row['file_id'], row['fname'], e)
            continue
        filename = row['disp_name'] or row['fname'] or ("attachment-%s" % row['file_id'])
        mime = row['mime'] or 'application/octet-stream'
        out.append((filename, mime, data, bool(row['inline'])))
    return out


# -------------------- Message build --------------------

def build_message(conn, row, include_attachments=True):
    hdr_msg = email.message_from_string(
        row['headers'] or '', policy=email.policy.default
    )
    msg = email.message.EmailMessage(policy=email.policy.default)

    for h in RELEVANT_HEADERS:
        if h in hdr_msg:
            for v in hdr_msg.get_all(h):
                msg[h] = v

    if 'Message-ID' not in msg:
        msg['Message-ID'] = row['mid'] or ("<osticket-%s@restore.local>" % row['entry_id'])
    if 'Date' not in msg:
        msg['Date'] = row['created'].strftime('%a, %d %b %Y %H:%M:%S +0200')

    body = row['body'] or ''
    fmt = (row['format'] or 'html').lower()
    subtype = 'html' if fmt == 'html' else 'plain'

    attachments = fetch_attachments(conn, row['entry_id']) if include_attachments else []

    if not attachments:
        msg.set_content(body, subtype=subtype, charset='utf-8')
        return msg

    msg.set_content(body, subtype=subtype, charset='utf-8')
    for filename, mime, data, is_inline in attachments:
        maintype, _, subtype_ = mime.partition('/')
        if not subtype_:
            maintype, subtype_ = 'application', 'octet-stream'
        try:
            msg.add_attachment(
                data, maintype=maintype, subtype=subtype_, filename=filename
            )
        except Exception as e:
            log.warning("entry_id=%s Attachment %s not included: %s",
                        row['entry_id'], filename, e)
    return msg


def serialize(msg):
    # Python 3.5 has a bug in the header folding of `policy.default`.
    # The `compat32` policy works around this and is sufficient for simply serialising
    # headers that have already been parsed.
    import email.policy
    return msg.as_bytes(policy=email.policy.compat32.clone(linesep='\r\n'))

# -------------------- Test mode --------------------

def test_one(args):
    conn = connect()
    try:
        if args.test_id:
            limit, entry_id, desc = None, args.test_id, False
        else:
            limit, entry_id, desc = 1, None, True  # newest M-Mail

        row = next(fetch_rows(conn, limit=limit, entry_id=entry_id,
                              order_desc=desc), None)
        if row is None:
            log.error("No matching email found (entry_id=%s)", args.test_id)
            return 1

        msg = build_message(conn, row,
                            include_attachments=not args.no_attachments)
        raw = serialize(msg)

        if args.test_out:
            with open(args.test_out, 'wb') as fh:
                fh.write(raw)
            log.info("Raw mail written: %s (%d bytes)", args.test_out, len(raw))
            return 0

        # Summary to stdout
        print("=" * 72)
        print("entry_id   : %s" % row['entry_id'])
        print("created    : %s" % row['created'])
        print("format     : %s" % row['format'])
        print("size bytes : %d" % len(raw))
        print("-" * 72)
        for h in ('From', 'To', 'Cc', 'Subject', 'Date',
                  'Message-ID', 'In-Reply-To', 'References'):
            v = msg.get(h)
            if v:
                print("%-11s: %s" % (h, v))
        print("-" * 72)
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                disp = part.get('Content-Disposition', '')
                fn = part.get_filename()
                payload = part.get_payload(decode=True)
                n = len(payload) if payload else 0
                print("part       : %-28s %8d bytes  %s%s" % (
                    ct, n, disp, (" filename=%s" % fn) if fn else ""
                ))
        else:
            payload = msg.get_payload(decode=True)
            n = len(payload) if payload else 0
            print("body       : %s, %d bytes" % (msg.get_content_type(), n))
        print("-" * 72)
        print("--- Roh-Quelltext ---")
        sys.stdout.buffer.write(raw)
        if not raw.endswith(b'\n'):
            sys.stdout.buffer.write(b'\n')
        print("--- End ---")
        return 0
    finally:
        conn.close()


# -------------------- Export mode --------------------

def write_eml(outdir, raw, created, entry_id):
    path = os.path.join(outdir, "%s.eml" % entry_id)
    with open(path, 'wb') as fh:
        fh.write(raw)
    ts = time.mktime(created.timetuple())
    os.utime(path, (ts, ts))


def make_archive(src_dir, archive_path):
    src_dir = src_dir.rstrip('/')
    parent = os.path.dirname(os.path.abspath(src_dir)) or '.'
    base = os.path.basename(src_dir)
    log.info("Creating archive %s ...", archive_path)
    with tarfile.open(archive_path, 'w:gz') as tar:
        tar.add(src_dir, arcname=base)
    size = os.path.getsize(archive_path)
    log.info("Archive complete: %s (%.1f MB)", archive_path, size / 1024.0 / 1024.0)


def export_all(args):
    since = datetime.strptime(args.since, '%Y-%m-%d') if args.since else None

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    log.info("Output directory: %s", outdir)

    conn = connect()
    total = ok = fail = 0
    try:
        for row in fetch_rows(conn, since=since, limit=args.limit,
                              entry_id=args.entry_id):
            total += 1
            try:
                msg = build_message(conn, row,
                                    include_attachments=not args.no_attachments)
                raw = serialize(msg)
                write_eml(outdir, raw, row['created'], row['entry_id'])
                ok += 1
                if total % 100 == 0 or total <= 20:
                    log.info("OK  id=%s  date=%s", row['entry_id'], row['created'])
            except Exception as e:
                fail += 1
                log.exception("Error with entry_id=%s: %s", row.get('entry_id'), e)
    finally:
        conn.close()

    log.info("Export done. total=%d ok=%d fail=%d", total, ok, fail)

    if ok == 0:
        log.error("No emails exported – no archive created.")
        return 1

    archive_path = args.archive or (outdir + ".tar.gz")
    make_archive(outdir, archive_path)

    if not args.keep_dir:
        log.info("Removing output directory (--keep-dir to keep it)")
        import shutil
        shutil.rmtree(outdir)

    return 0


# -------------------- main --------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true',
                    help='Build newest M-Mail and output it')
    ap.add_argument('--test-id', type=int, default=None,
                    help='Build and output a specific email (thread_entry.id)')
    ap.add_argument('--test-out', default=None,
                    help='Writing raw mail to a file instead of stdout')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--since', default=None, help='YYYY-MM-DD')
    ap.add_argument('--entry-id', type=int, default=None)
    ap.add_argument('--no-attachments', action='store_true')
    ap.add_argument('--outdir', default=DEFAULT_OUTDIR)
    ap.add_argument('--archive', default=None,
                    help='Archive path (Default: <outdir>.tar.gz)')
    ap.add_argument('--keep-dir', action='store_true',
                    help='Keep .eml-directory')
    args = ap.parse_args()

    if args.test or args.test_id:
        return test_one(args)
    return export_all(args)


if __name__ == '__main__':
    sys.exit(main())
