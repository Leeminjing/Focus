# Desktop directory layout

The client uses Electron and plain HTML/CSS/JavaScript. This layout groups existing
files by responsibility; browser scripts still load in their original order and
publish the same `Focus*` globals.

```text
desktop/
  package.json              Electron entry and npm commands
  compose.yaml              Local data services
  src/
    main/                   Electron host, startup, notifications, native sandbox
    preload/                Electron capability bridge
    renderer/
      index.html            Workbench entry
      desktop-patrol.html   Companion window entry
      splash.html           Startup window entry
      app/                  Bootstrap, navigation and application coordination
      features/
        conversation/       Messages, composer, access review and retry
        context/            Context editing, compression and run previews
        patrol/
          workspace/        Workspace controller, inputs, feedback and observation
          companion/        Desktop robot and input panel
          editor/           Patrol authoring and workbench
          avatar/           Avatar and presence
        loop/               Loop API, state, live connection and console
        map/                Graph layout and views
        materials/          Material loading, grouping and pickers
        files/              File previews, artifacts, changes and timeline
        settings/           Storage and session settings
        memory/             Memory and strategy UI
        plugins/            Plugin UI
        skills/             Skill picker
      shared/               HTTP, i18n, Markdown, UI, streams and common styles
      assets/               Images, fonts and icons
      vendor/               Vendored browser dependencies
  tests/
    integration/            Application and cross-feature regressions
    e2e/                    Electron, browser and backend-connected scenarios
    fixtures/               Test preloads, fixture pages and data
    helpers/                Shared VM and DOM harnesses
  scripts/                  Builds, test runner, captures and benchmarks
```

Unit tests live beside the module or feature they exercise. Feature-specific CSS
lives with its feature; shared tokens and shell styles live in `shared/styles/`.
The compiled Patrol editor and its license remain beside the editor source.

## Commands

Run from `desktop/`:

```sh
npm ci
npm run install:electron -- --no
npm start
npm test
npm run build:patrol-editor
```

`npm test` discovers `*.test.cjs` and `*.test.js` in `src/` and `tests/`, including
tests that launch an isolated Electron instance. `npm run test:native` runs the
Windows native sandbox probes separately. Backend-connected E2E scenarios retain
their individual setup requirements.

The Gateway serves `src/renderer/` at the existing `/desktop/` URL. Electron
loads `src/main/main.cjs`; it resolves repository and desktop paths from the
desktop package root. Keep new source files in their owning feature rather than
adding them to the package root.
