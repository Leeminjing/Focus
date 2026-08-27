"use strict";

const assert = require("node:assert");
const vm = require("node:vm");

globalThis.window = globalThis;
globalThis.location = {
  origin: "http://127.0.0.1:18081",
  pathname: "/bridge/plugin-config/session-1/load-token-1/token-1/index.html",
  search: "?lang=zh-CN&theme-type=light",
};
globalThis.addEventListener = () => {};
globalThis.setInterval = () => null;
globalThis.AscCommon = { g_dKoef_mm_to_pix: 3.78 };
const posted = [];
const requested = [];
globalThis.top = { postMessage: message => posted.push(message) };
globalThis.fetch = async url => {
  requested.push(String(url));
  if (String(url).includes("/next?")) return new Promise(() => {});
  return { ok: true, json: async () => ({}) };
};
globalThis.Asc = {
  scope: {},
  plugin: {
    attachedEvents: [],
    attachEditorEvent(name, callback) { this.attachedEvents.push([name, callback]); },
    callCommand(source, _recalculate, _force, callback) {
      globalThis.lastCommandSource = source;
      // ONLYOFFICE serializes this function before running it in editor context.
      const isolated = vm.runInNewContext(`(${source.toString()})`, {
        Api: globalThis.Api,
        Asc: { scope: globalThis.Asc.scope },
        AscCommon: globalThis.AscCommon,
        editor: globalThis.editor,
      });
      const result = isolated();
      try {
        callback(JSON.parse(JSON.stringify(result)));
      } catch {
        callback(undefined);
      }
    },
  },
};

const drawingDocument = {
  m_lTargetPage: 0,
  m_arrPages: [{
    drawingPage: { left: 10, top: 20, right: 210, bottom: 320 },
    width_mm: 200,
    height_mm: 300,
    selectionArray: [{ x: 20, y: 30, w: 40, h: 10 }],
  }],
  TargetHtmlElementLeft: 30,
  TargetHtmlElementTop: 50,
  m_dTargetSize: 5,
  m_oWordControl: { m_nZoomValue: 100 },
  ConvertCoordsFromCursor2: (x, y) => ({ Page: 0, X: x - 10, Y: y - 20 }),
};
const hitParagraph = {
  Get_Id: () => "internal-paragraph-1",
  GetText: () => "clicked paragraph",
  GetIndex: () => 4,
  GetParent: () => null,
};
const logicDocument = {
  DrawingObjects: {
    getGraphicInfoUnderCursor: () => ({}),
  },
  HdrFtr: { IsInText: () => null },
  Footnotes: { CheckHitInFootnote: () => false },
  Endnotes: { CheckHitInEndnote: () => false },
  GetPageContentFrame: () => ({ X: 20, Y: 25, XLimit: 180, YLimit: 275 }),
  IsInText: () => hitParagraph,
  IsTableBorder: () => null,
};
globalThis.editor = { WordControl: { m_oDrawingDocument: drawingDocument, m_oLogicDocument: logicDocument } };

let currentDocument;
globalThis.Api = { GetDocument: () => currentDocument };
require("./onlyoffice-focus-bridge/code.js");

function paragraph(text = "paragraph", parentType = "document") {
  const parent = { GetClassType: () => parentType, GetIndex: () => 0 };
  const style = { GetName: () => "Normal" };
  style.owner = style;
  return {
    GetText: () => text,
    GetClassType: () => "paragraph",
    GetParent: () => parent,
    GetId: () => `${parentType}-${text}`,
    GetStyle: () => style,
  };
}

function documentFor(kind) {
  const para = ["paragraph", "header", "footer"].includes(kind)
    ? paragraph(kind, kind === "paragraph" ? "document" : kind)
    : null;
  const selection = kind === "text_range" ? { GetText: () => "selected text", GetId: () => "range-1" } : { GetText: () => "" };
  const cell = kind === "cell" ? {
    GetText: () => "cell text", GetId: () => "cell-1",
    GetRowIndex: () => 2, GetColumnIndex: () => 3,
  } : null;
  const table = ["table", "cell"].includes(kind) ? {
    GetText: () => "table text", GetId: () => "table-1",
    GetCurrentCell: () => cell,
  } : null;
  const drawing = ["image", "shape"].includes(kind) ? {
    GetText: () => "", GetId: () => `${kind}-1`,
    GetClassType: () => kind === "image" ? "image" : "drawing shape",
  } : null;
  return {
    GetRangeBySelect: () => selection,
    GetCurrentParagraph: () => para,
    GetCurrentTable: () => table,
    GetCurrentDrawing: () => drawing,
    GetAllParagraphs: () => para ? [para] : [],
    GetAllTables: () => table ? [table] : [],
    GetId: () => "document-1",
  };
}

async function observe(kind) {
  currentDocument = documentFor(kind);
  posted.length = 0;
  globalThis.Asc.plugin.event_onTargetPositionChanged();
  await new Promise(resolve => setImmediate(resolve));
  return posted.find(message => message.type === "focus-docx-target")?.target;
}

(async () => {
  currentDocument = documentFor("paragraph");
  posted.length = 0;
  globalThis.Asc.plugin.init();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(
    posted.some(message => message.type === "focus-docx-target"),
    "plugin init must publish an initial semantic target",
  );
  assert.strictEqual(
    posted.find(message => message.type === "focus-docx-target").target.format.style,
    "Normal",
    "ApiStyle must cross callCommand only as its serializable name",
  );
  assert.ok(
    requested.some(url => url.includes("/bridge/events/session-1?access_token=token-1")),
    "event reporting must use the unmodified command token from the signed path",
  );
  assert.ok(
    requested.some(url => url.includes("/bridge/commands/session-1/next?timeout=25&access_token=token-1")),
    "command polling must use the unmodified command token from the signed path",
  );
  assert.ok(requested.every(url => !url.includes("theme-type") && !url.includes("lang=zh-CN")));

  for (const kind of ["paragraph", "text_range", "table", "cell", "image", "shape", "header", "footer", "page_region"]) {
    const target = await observe(kind);
    assert.ok(target, `${kind} observation must be published`);
    assert.strictEqual(target.kind, kind);
    assert.match(target.target_id, new RegExp(`^${kind}:`));
    assert.ok(Array.isArray(target.structure_path));
  }

  currentDocument = documentFor("paragraph");
  posted.length = 0;
  globalThis.Asc.plugin.event_onTargetPositionChanged();
  await new Promise(resolve => setImmediate(resolve));
  const first = posted.find(message => message.type === "focus-docx-projection").projection;
  drawingDocument.m_arrPages[0].drawingPage.left = 40;
  drawingDocument.m_arrPages[0].drawingPage.right = 340;
  drawingDocument.m_arrPages[0].selectionArray = [{ x: 40, y: 60, w: 80, h: 20 }];
  drawingDocument.m_oWordControl.m_nZoomValue = 150;
  posted.length = 0;
  globalThis.Asc.plugin.event_onTargetPositionChanged();
  await new Promise(resolve => setImmediate(resolve));
  const refreshed = posted.find(message => message.type === "focus-docx-projection").projection;
  assert.notDeepStrictEqual(refreshed.viewport_rect, first.viewport_rect);
  assert.strictEqual(refreshed.zoom, 1.5);

  function executeAnchorPoint(command) {
    globalThis.Asc.scope.focusCommand = command;
    const isolated = vm.runInNewContext(`(${globalThis.lastCommandSource.toString()})`, {
      Api: globalThis.Api,
      Asc: { scope: globalThis.Asc.scope },
      AscCommon: globalThis.AscCommon,
      editor: globalThis.editor,
    });
    return JSON.parse(JSON.stringify(isolated()));
  }

  drawingDocument.m_arrPages[0].drawingPage = { left: 10, top: 20, right: 210, bottom: 320 };
  drawingDocument.m_oWordControl.m_nZoomValue = 100;
  const contentPoint = executeAnchorPoint({
    action: "resolve_anchor_point",
    expected_version: 1,
    arguments: { viewport_x: 60, viewport_y: 170 },
  });
  assert.deepStrictEqual(contentPoint.projection.point, { x: .25, y: .5 });
  assert.strictEqual(contentPoint.projection.page, 1);
  assert.strictEqual(contentPoint.observation.target_id, "paragraph:internal-paragraph-1");
  assert.strictEqual(contentPoint.observation.content, "clicked paragraph");

  drawingDocument.m_oWordControl.X = 50;
  drawingDocument.m_oWordControl.Y = 100;
  drawingDocument.GetMainOffset = () => ({ x: 20, y: 30 });
  const offsetPoint = executeAnchorPoint({
    action: "resolve_anchor_point", expected_version: 1,
    arguments: { viewport_x: 130, viewport_y: 300 },
  });
  assert.deepStrictEqual(offsetPoint.projection.point, { x: .25, y: .5 },
    "iframe coordinates must remove WordControl and ruler offsets before normalization");
  drawingDocument.m_oWordControl.X = 0;
  drawingDocument.m_oWordControl.Y = 0;
  drawingDocument.GetMainOffset = () => ({ x: 0, y: 0 });

  let wrongTargetChanged = false;
  currentDocument.GetCurrentParagraph().SetBold = () => { wrongTargetChanged = true; };
  const wrong = executeAnchorPoint({ action: "edit", target_id: "paragraph:does-not-exist",
    expected_version: 1, operation: "set_text_format", arguments: { bold: true } });
  assert.strictEqual(wrong.changed, false, "unknown target must not edit the current caret paragraph");
  assert.strictEqual(wrongTargetChanged, false);

  logicDocument.IsInText = () => null;
  const blankPoint = executeAnchorPoint({
    action: "resolve_anchor_point",
    expected_version: 1,
    arguments: { viewport_x: 180, viewport_y: 290 },
  });
  assert.strictEqual(blankPoint.observation.kind, "page_region");
  assert.deepStrictEqual(blankPoint.projection.point, { x: .85, y: .9 });
  assert.strictEqual(blankPoint.projection.rect.width, 0);
  assert.strictEqual(blankPoint.projection.rect.height, 0);

  const pageAnchor = { spatial_id: "blank", region: {
    target: { target_id: blankPoint.observation.target_id, kind: "page_region" },
    placement: { page: 1, x: .85, y: .9 },
  } };
  const projectAnchors = anchors => executeAnchorPoint({ action: "project_anchors",
    expected_version: 1, arguments: { anchors } }).evidence.projections;
  assert.strictEqual(projectAnchors([pageAnchor])[0].viewport_rect.y, 290);
  drawingDocument.m_arrPages[0].drawingPage.top -= 80;
  drawingDocument.m_arrPages[0].drawingPage.bottom -= 80;
  assert.strictEqual(projectAnchors([pageAnchor])[0].viewport_rect.y, 210,
    "page anchor follows scroll without a caret event");

  let targetPage = 0;
  let targetDeleted = false;
  const raw = { GetPagesCount: () => 1, GetAbsolutePage: () => targetPage,
    GetPageBounds: () => ({ Left: 20, Top: 30, Right: 120, Bottom: 50 }),
    IsUseInDocument: () => !targetDeleted };
  const para = { Paragraph: raw, GetInternalId: () => "fixed", GetClassType: () => "paragraph",
    SetBold: () => {} };
  globalThis.Api.GetByInternalId = id => id === "fixed" ? para : null;
  drawingDocument.m_arrPages[1] = { width_mm: 200, height_mm: 300,
    drawingPage: { left: 10, top: 400, right: 210, bottom: 700 } };
  const attached = { spatial_id: "first", region: {
    target: { target_id: "paragraph:fixed", kind: "paragraph" },
    placement: { page: 1, x: .35, y: .12 },
    attachment: { page_offset: 0, x: .5, y: .3 },
  } };
  const second = JSON.parse(JSON.stringify(attached));
  second.spatial_id = "second";
  second.region.attachment.x = .8;
  targetPage = 1;
  const moved = projectAnchors([attached, second]);
  assert.deepStrictEqual(moved.map(item => item.page), [2, 2]);
  assert.deepStrictEqual(moved.map(item => item.viewport_rect.x), [80, 110]);
  assert.strictEqual(moved[0].viewport_rect.y, 436,
    "semantic attachment follows its object across pages");
  const wrongKind = executeAnchorPoint({ action: "edit", target_id: "table:fixed",
    expected_version: 1, operation: "set_text_format", arguments: { bold: true } });
  assert.strictEqual(wrongKind.changed, false, "a matching id with the wrong kind is not a valid target");
  targetDeleted = true;
  assert.strictEqual(projectAnchors([attached])[0].projection_unavailable, true,
    "objects retained in the engine id table after deletion must not remain valid anchors");

  console.log("docx-onlyoffice-bridge: isolated commands, target kinds, and projection refresh passed");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
