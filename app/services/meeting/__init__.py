"""The meeting workspace.

- :mod:`.clients` — the Meeting integration, wired from settings.
- :mod:`.context` — what each request runs with; Meeting's failures as
  application errors.
- :mod:`.delegation` — acting at Meeting as the signed-in user.
- :mod:`.hosted` — meetings hosted by Meeting: list, read, flag, delete, edit.
- :mod:`.minutes` — minutes: read, generate, translate, draft, ask.
- :mod:`.recordings` — live and uploaded recordings, before the archiver
  hands them to Meeting.
- :mod:`.templates` / :mod:`.template_import` — summary templates, and
  reading one out of an uploaded document.
- :mod:`.vocabulary` — the terms speech recognition should get right.
- :mod:`.model` / :mod:`.settings` / :mod:`.chinese` — the system model,
  the deployment's choice of who writes minutes, and script conversion.
"""
