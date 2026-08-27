# Spatial bridge source patch

The browser plugin first uses the public ONLYOFFICE Plugin/Office APIs. Public APIs
do not expose a stable page-space rectangle for every Word object. The optional
patch in this directory is therefore limited to exporting already-computed layout
rectangles from the open-source editor to the Focus Bridge event channel. It must
not alter OOXML parsing, layout, pagination, editing, history, or save behavior.

The patch is kept as source so deployments that enable it continue to satisfy the
GNU AGPL v3.0 source-availability requirement. The unpatched Community Edition
remains usable for editing; spatial overlays report `projection_unavailable` for
targets whose rectangle cannot be obtained through the public API.
