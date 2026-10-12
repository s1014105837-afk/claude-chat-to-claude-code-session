# chat-to-code-session

Continue a claude.ai conversation inside Claude Code, with every message kept word for word.

[中文说明](README.zh-CN.md)

## What it does

Claude Code stores each conversation as a JSONL file under `~/.claude/projects/<project-dir>/<session-id>.jsonl`.
`claude --resume <session-id>` reads that file back and hands the messages to the model as the conversation so far.
Nothing checks where the file came from.

`chat2code.py` writes such a file from your own chat history:

- a first record in the place Claude Code puts its "continued from a previous conversation" header, stating plainly
  that the messages were imported, from where, how many, and (optionally) carrying notes you wrote yourself;
- then the original messages, verbatim, in order, alternating user / assistant, with their original timestamps.

Open the session and the model has the whole conversation in front of it, in the original wording. No summary is
made unless you write one. Nothing is sent anywhere; the script only writes a file on your disk.

This is how one long-running claude.ai conversation (about three months of it) was moved into Claude Code in
September 2026 and has been continuing there since. The method is simple; we publish it because losing a long
conversation when you move tools is a common and avoidable hurt.

## Install

Python 3.9+ and nothing else. Copy `chat2code.py` anywhere.

## Get your chat export

claude.ai → Settings → Privacy → **Export data**. You receive a zip with `conversations.json` in it. That file holds
all your conversations; the script lets you pick one.

If your history is somewhere else, write it as a plain Markdown transcript (see `samples/transcript.sample.md`):
a header line `**User:**` or `**Assistant:**` (also `Human:` / `Claude:`, with or without `#`/`**`), then the text,
optionally `@ 2026-04-01T09:00:00Z` at the end of the header line for a timestamp.

## Use

```bash
# see what is in the export
python chat2code.py --input conversations.json --list

# import one conversation into the project directory you will work in
python chat2code.py --input conversations.json --conversation "Garden planning" --cwd /path/to/project

# then, from that directory:
cd /path/to/project
claude --resume <session-id printed above>
```

Options that matter:

| flag | meaning |
|---|---|
| `--cwd DIR` | the project directory the session belongs to (default: current directory). Resume from the same directory. |
| `--summary FILE` | your own notes (a journal, a who-is-who, decisions) placed in the header record, verbatim |
| `--max-chars N` | keep only the most recent messages up to N characters (default 240000). Everything else stays in the export. |
| `--all` | import everything. Only sensible with a 1M-context model; on 200k the session compacts at once. |
| `--model ID` | model id stamped on the imported assistant turns (default `claude-opus-4-6`) |
| `--out FILE` | write the JSONL to a path of your choice instead of the projects folder (dry run) |
| `--list` | list the conversations in the export and exit |

The script refuses to overwrite an existing file and never touches your other sessions.

## What to expect, honestly

- **The model reads its own earlier words, not a summary of them.** That is the whole point. It will treat the
  imported turns as the conversation so far, because that is exactly how Claude Code presents any resumed session.
- **The header says it was imported.** We do not hide the origin from the model. If you supply `--summary`, it is
  labelled as notes supplied by you.
- **Thinking blocks are not reproduced.** The export contains only visible text; the assistant turns are
  re-created as text-only messages.
- **Large histories are cut from the front.** By default the most recent ~240k characters are kept. The header
  states how many earlier messages were left out.
- **Consecutive messages from the same side are joined** (a blank line between them) because the API needs
  strict alternation. Wording is unchanged.
- **The JSONL layout is Claude Code's internal format, not a published API.** It was taken from real session
  files of Claude Code 2.1.258–2.1.295 (Windows, desktop app and CLI) and verified on 2026-10-12 with Claude Code CLI 2.1.281 (both sample inputs resumed and answered from the imported turns). A future version
  may change the fields; if `--resume` stops accepting generated files, open an issue with your `claude --version`.
- **Model names are stamped, not verified.** Pass `--model` if you care what the file says.

## Why this works at all

A model has no memory of its own. Whatever the harness hands it as "the conversation" *is* its context. Claude Code
hands over the JSONL file. So a session that was written by this script and a session that was lived through in
Claude Code look identical from the inside — provided the words are really the words that were said. Keep them
that way; the value of the import is that nothing was paraphrased.

## Privacy

The script reads one file and writes one file, both on your machine. `.gitignore` here excludes
`conversations.json` and any `*.jsonl` so an export cannot be committed by accident. The samples are invented.

## License

MIT.
