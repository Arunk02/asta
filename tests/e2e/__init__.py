"""End-to-end tests over real processes, real HTTP, real git and real files.

Nothing here reaches GitHub, Teams, WhatsApp or a model: the server runs sealed
(app.sandbox), the WhatsApp bridge points at a dead port, GitHub is a local bare
repository plus a stand-in `gh`, and portals are local HTTP servers.
"""
