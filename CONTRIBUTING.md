# Contributing

Thank you for helping. Issues and pull requests are welcome at
<https://github.com/TicketWaveHQ/serverless-backup-sentinel>.

Please report security problems privately, as [SECURITY.md](SECURITY.md) describes, not as public
issues.

## What a change must keep

1. **Zero dependencies.** `handler.py` imports only the Python standard library, and a test
   enforces it. The sentinel is the one component that has to keep working when other things have
   broken, and every dependency is another thing that can break.
2. **One file.** `handler.py` stays a single file that can be zipped and uploaded on its own.
3. **"Cannot verify" is an alarm.** Any new way for a check to fail must end in a result with
   `alarm` set to `True`. It must never raise out of `evaluate()`, and never report as `ok`.
4. **Tests are offline and model real events.** Tests replace the network connection
   (`handler._urlopen`), not the code that uses it. If you add support for a platform or a
   trigger, add a test built from the event that platform really sends, with any secrets removed.
5. **Nothing secret in output.** Keys, tokens and webhook URLs never appear in replies, logs or
   error messages.
6. **Python 3.10 or later.**

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

No install, credentials or network are needed. CI runs the same command on Python 3.10 to 3.14.

## Pull requests

- Keep each pull request to one change, with tests for any new behaviour.
- Update the configuration tables in [README.md](README.md) when you add or change a variable,
  and add a line under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).
- Write documentation in UK English and plain words. Do not use em-dashes: CI rejects them.

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE).
