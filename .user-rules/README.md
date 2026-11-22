# .user-rules

Rules for this project that the user's own tooling reads: the git hooks of the
user's dotfiles repository. Other tools ignore this directory. Change these
files in this project and commit them here.

| Path | Read by | Contents |
|---|---|---|
| `commit/config` | the dotfiles git hooks | Commit rules in git config syntax: `message.types`, `message.scope`, `message.breaking`, `message.language`, `message.coAuthoredBy`, `time.zone`, `time.name`, `encrypted.path` |
| `commit/secret-scan-allow` | the secret scan of those hooks | Optional. Confirmed false positives, one text per line, the reason after `#` |
