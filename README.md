# osTicket E-Mail Export

Export incoming osTicket messages (`thread_entry.type='M'`) as `.eml` files
and bundle them into a `.tar.gz` archive for re-import into an IMAP server.

## Why

Foolishly i configured to **delete messages from the mail server after
fetching**, the original messages no longer exist on the IMAP server. 
Luckily they where still in the osTicket DB, though — in pieces. 
This script puts the pieces back together into valid RFC822 messages.

## Requirements

- Python 3.5+
- `PyMySQL` matching the Python version:
  - Python 3.5: `sudo apt-get install python3-pymysql` (or `pip install "PyMySQL==0.10.1"`)
  - Python 3.6+: `pip install pymysql`
- Read access to the osTicket database and attachments directory

## Usage

```bash
# Inspect the latest M-message (no file written, prints summary + raw to stdout)
python3 export_osticket_emls.py --test

# Inspect a specific thread_entry.id, dump raw message to file
python3 export_osticket_emls.py --test-id 6688 --test-out /tmp/test.eml

# Export the first 5 messages, keep the .eml directory
python3 export_osticket_emls.py --limit 5 --outdir /tmp/test-export --keep-dir

# Full export
python3 export_osticket_emls.py --outdir /tmp/osticket-eml-export
```

Flags:

| Flag | Purpose |
|------|---------|
| `--test` | Build the most recent M-message and print it |
| `--test-id N` | Build message with `thread_entry.id=N` |
| `--test-out FILE` | Write raw message to FILE instead of stdout |
| `--limit N` | Export only N messages |
| `--since YYYY-MM-DD` | Export messages on or after this date |
| `--entry-id N` | Export only this `thread_entry.id` |
| `--no-attachments` | Skip attachments |
| `--outdir DIR` | Output directory (default: `osticket-eml-export`) |
| `--archive FILE` | Archive path (default: `<outdir>.tar.gz`) |
| `--keep-dir` | Keep `.eml` directory after archiving |

## What it produces

- One `<thread_entry_id>.eml` file per incoming message
- `mtime` of each file = original `created` timestamp
- A `<outdir>.tar.gz` containing the whole directory

## Database layout (osTicket 1.16.x)

The script was written against the following schema. If your osTicket
version differs, adapt the queries accordingly.

### Relevant tables

- `ost_thread_entry` — one row per message/note/response
  - `id`, `type` (`M`=incoming, `R`=outgoing, `N`=note), `format` (`html`/`text`),
    `body` (the parsed body only), `created`, `title` (often NULL)
- `ost_thread_entry_email` — one row per *incoming* mail, header only
  - `thread_entry_id`, `mid` (Message-ID), `headers` (full raw header block)
  - **Only `M` entries have a row here.** Outgoing `R` messages are
    generated on the fly by osTicket at send time and never persisted.
- `ost_attachment` — links a file to a thread entry
  - `object_id` = `ost_thread_entry.id`, `type='H'` for email attachments,
    `file_id`, `name`, `inline`
- `ost_file` — file metadata
  - `id`, `bk` (`F`=filesystem, `D`=database), `key` (filesystem path component),
    `name`, `type` (mime), `size`
- `ost_file_chunk` — chunks for DB-stored files
  - `file_id`, `chunk_id`, `filedata`

### Reconstruction rules

1. **Message-ID / Subject / From / To / Date** come from
   `ost_thread_entry_email.headers` (parsed as an RFC822 header block).
   The `title` column is unreliable — often NULL.
2. **Body** comes from `ost_thread_entry.body`, encoded as `text/html` or
   `text/plain` depending on `ost_thread_entry.format`.
3. **Attachments** are looked up via `ost_attachment` with
   `type='H'` and `object_id = thread_entry.id`.
   - `ost_file.bk='F'` → file at `ATTACH_ROOT/<key[0]>/<key>`
   - `ost_file.bk='D'` → chunks from `ost_file_chunk`, ordered by `chunk_id`
4. Only `type='M'` rows that also have a row in `ost_thread_entry_email`
   are exported (INNER JOIN in `fetch_rows`).

### What is *not* recovered

- Non-relevant headers (Return-Path, Received, DKIM, spam scores). Only the
  headers in `RELEVANT_HEADERS` are copied.
- Original MIME structure (`multipart/alternative`, encodings). Body is
  always attached as a single part; attachments go into `multipart/mixed`.
- Outgoing responses (`R`) — osTicket never stored their raw form.
- Internal notes (`N`) — not emails.
