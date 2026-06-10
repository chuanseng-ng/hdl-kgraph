"""Filelist (.f / .vc) parser (M2).

Planned syntax support, matching common simulator conventions:

* one source path per line, comments (``//``, ``#``)
* ``+incdir+<dir>`` and ``+define+<NAME>[=<value>]``
* nested filelists via ``-f <file>`` (cycles detected)
* ``-y <dir>`` library dirs and ``-v <file>`` library files
* environment variable expansion (``$VAR`` / ``${VAR}``)

Produces FILELIST nodes; file ordering is preserved because compilation order
matters for ``define`` visibility.
"""
