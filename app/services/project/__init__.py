"""Non-trivial project logic.

Submodule responsibilities:

- :mod:`~app.services.project.ownership` — which project a caller may use;
  shared by the project and session endpoints.
- :mod:`~app.services.project.files` — a project's files: adding one from
  either source, removing one, and reading the current version of each.

``files`` is imported by module path (``from app.services.project import
files as project_files``), like :mod:`app.services.schedule.files`: its
verbs read only beside the module's name.
"""
