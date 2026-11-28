"""The Meeting service (hkmeeting.net): its HTTP protocol, and only that.

- :mod:`.transport` — sessions, headers, the response envelope, cookie
  rotation. Every call goes through it.
- :mod:`.output` — the streamed text some endpoints answer with.
- :mod:`.records` — Meeting's answers, whose shape varies, read into
  typed records.
- :mod:`.storage` — putting a recording into Meeting's object storage.
- :mod:`.accounts` — sign-in, sign-out, registration, password reset.
- :mod:`.meetings` — meetings, transcripts, minutes, translation, uploads.

Two features use it: sign-in (:mod:`app.services.identity.provider`) and
the meeting workspace (:mod:`app.services.meeting`).
"""
