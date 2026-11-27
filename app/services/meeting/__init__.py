"""The meeting workspace.

- :mod:`.clients` — the Meeting integration, wired from settings.
- :mod:`.context` — what each request runs with; Meeting's failures as
  application errors.
- :mod:`.records` — reading Meeting's records, whose shape varies.
- :mod:`.hosted` — meetings hosted by Meeting: list, read, flag, delete, edit.
- :mod:`.minutes` — minutes: read, generate, translate, draft, ask.
- :mod:`.model` / :mod:`.settings` / :mod:`.chinese` — the system model,
  the deployment's choice of who writes minutes, and script conversion.
"""
