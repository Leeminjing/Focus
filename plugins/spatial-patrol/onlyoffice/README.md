# Focus spatial-patrol DOCX runtime

This directory owns the optional ONLYOFFICE Docs Community Edition runtime used by
the `spatial-patrol` plugin. Focus itself never depends on this Compose project.
The plugin starts it lazily when a DOCX editing session is opened and may stop it
after the configured idle period.

The image is pinned to `onlyoffice/documentserver:9.4.0`. ONLYOFFICE Docs Community
Edition is distributed under GNU AGPL v3.0. The corresponding upstream source is:

- https://github.com/ONLYOFFICE/DocumentServer/tree/v9.4.0
- https://github.com/ONLYOFFICE/sdkjs/tree/v9.4.0

Focus Bridge plugin sources are shipped in `desktop/onlyoffice-focus-bridge/`.
Any spatial bridge patch is shipped as source in `source-patch/`; no proprietary
Document Server binary or font is included in this repository.

To inspect the resolved configuration without starting anything:

```powershell
docker compose --env-file .runtime/documentserver.env -f compose.yaml config
```
