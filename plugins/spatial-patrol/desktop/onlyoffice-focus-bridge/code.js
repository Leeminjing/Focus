/* GNU AGPL-compatible Focus Bridge for ONLYOFFICE Docs Community Edition.
   Inputs are scoped editor commands and exact iframe viewport points; outputs are
   JSON-only observations, page placements and edit evidence. Point resolution uses
   the fixed 9.4.0 open-source hit-test surfaces without moving the caret or invoking
   document mouse actions, so establishing an anchor cannot modify the document. */
(function () {
  "use strict";

  const pathParts = location.pathname.split("/").filter(Boolean);
  const configIndex = pathParts.lastIndexOf("plugin-config");
  const bridgeOrigin = location.origin;
  const sessionId = configIndex >= 0 ? decodeURIComponent(pathParts[configIndex + 1] || "") : "";
  const accessToken = configIndex >= 0 ? decodeURIComponent(pathParts[configIndex + 3] || "") : "";
  let documentVersion = 1;
  let polling = false;
  let stopped = false;
  let activeTargetId = "selection:1";
  let activeTargetKind = "text_range";
  let trackedAnchors = [];
  let projectionTimer = null;
  let projecting = false;
  let lastProjection = "";
  const editorCommands = [];
  let editorBusy = false;

  function endpoint(path) {
    const separator = path.includes("?") ? "&" : "?";
    return `${bridgeOrigin}${path}${separator}access_token=${encodeURIComponent(accessToken)}`;
  }

  async function send(path, body) {
    const response = await fetch(endpoint(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`Focus Bridge HTTP ${response.status}`);
    return response.json();
  }

  function callEditor(command, callback) {
    editorCommands.push({ command, callback });
    drainEditorCommands();
  }

  function drainEditorCommands() {
    if (editorBusy || !editorCommands.length || stopped) return;
    editorBusy = true;
    const next = editorCommands.shift();
    runEditor(next.command, result => {
      editorBusy = false;
      try { next.callback(result); } finally { drainEditorCommands(); }
    });
  }

  function runEditor(command, callback) {
    Asc.scope.focusCommand = command;
    window.Asc.plugin.callCommand(function () {
      const command = Asc.scope.focusCommand;
      const fail = message => ({ changed: false, document_version: command.expected_version, error: message });
      const hashText = text => {
        let hash = 2166136261;
        for (let index = 0; index < text.length; index += 1) {
          hash ^= text.charCodeAt(index);
          hash = Math.imul(hash, 16777619);
        }
        return (hash >>> 0).toString(16).padStart(8, "0");
      };
      const callValue = (object, names, fallback = null) => {
        for (const name of names) {
          try {
            if (typeof object?.[name] === "function") return object[name]();
          } catch {}
        }
        return fallback;
      };
      const stableIdOf = object => {
        const value = callValue(object, ["GetInternalId", "Get_Id", "GetId"], null);
        return value === null || value === undefined ? null : String(value);
      };
      const textOf = object => {
        const value = callValue(object, ["GetText", "GetSelectedText"], "");
        return value === null || value === undefined ? "" : String(value).slice(0, 1024);
      };
      const indexOf = object => {
        const value = Number(callValue(object, ["GetIndex", "Get_AbsoluteStartPage"], 0));
        return Number.isInteger(value) && value >= 0 ? value : 0;
      };
      const lineageOf = object => {
        const lineage = [];
        let current = object;
        for (let depth = 0; current && depth < 10; depth += 1) {
          lineage.push(current);
          let parent = null;
          try { parent = current.GetParent?.() || current.Parent || current.parent || null; } catch {}
          if (!parent || parent === current) break;
          current = parent;
        }
        return lineage;
      };
      const targetObservation = (kind, object, paragraph, structurePath = []) => {
        const stableId = stableIdOf(object) || stableIdOf(paragraph);
        const exact = textOf(object) || textOf(paragraph);
        const targetId = `${kind}:${stableId || hashText(JSON.stringify([structurePath, exact]))}`;
        return {
          target_id: targetId,
          kind,
          content: exact,
          stable_id: stableId,
          structure_path: structurePath,
          fingerprint: {
            exact,
            prefix: exact.slice(0, 128),
            suffix: exact.slice(-128),
            style: null,
          },
        };
      };
      const rawObject = object => object?.Paragraph || object?.Table || object?.Cell || object?.Drawing || object;
      const liveObject = object => {
        const raw = rawObject(object);
        return !!raw && (typeof raw.IsUseInDocument !== "function" || raw.IsUseInDocument());
      };
      const resolveObject = targetId => {
        if (typeof targetId !== "string" || !targetId.includes(":")) return null;
        const kind = targetId.slice(0, targetId.indexOf(":"));
        const matchesKind = object => {
          const type = String(object?.GetClassType?.() || "").toLowerCase();
          if (["paragraph", "header", "footer"].includes(kind)) return type === "paragraph";
          if (kind === "cell") return type === "tablecell" || type === "cell";
          if (["image", "shape"].includes(kind)) return type === kind || type === "drawing";
          return type === kind;
        };
        const id = targetId.slice(targetId.indexOf(":") + 1);
        const object = Api.GetByInternalId?.(id);
        if (object) return liveObject(object) && matchesKind(object) ? object : null;
        const doc = Api.GetDocument();
        const candidates = [...(doc.GetAllParagraphs?.() || []), ...(doc.GetAllTables?.() || [])];
        const matches = candidates.filter(item => stableIdOf(item) === id && liveObject(item) && matchesKind(item));
        return matches.length === 1 ? matches[0] : null;
      };
      const layoutBoxes = object => {
        let raw = rawObject(object);
        if (raw?.Content && !raw.GetPageBounds && raw.Content.GetPageBounds) raw = raw.Content;
        const count = raw?.GetPagesCount?.() || raw?.Pages?.length || 0;
        const boxes = [];
        for (let index = 0; index < count; index += 1) {
          const bounds = raw.GetPageBounds?.(index);
          const page = raw.GetAbsolutePage?.(index);
          if (bounds && Number.isInteger(page) && page >= 0 && bounds.Right > bounds.Left && bounds.Bottom > bounds.Top) {
            boxes.push({ page, page_offset: index, x: bounds.Left, y: bounds.Top,
              width: bounds.Right - bounds.Left, height: bounds.Bottom - bounds.Top });
          }
        }
        return boxes;
      };
      const pageGeometry = (drawing, page) => {
        const offset = drawing.GetMainOffset?.() || { x: 0, y: 0 };
        const control = drawing.m_oWordControl;
        return {
          x: page.drawingPage.left + Number(control.X || 0) + offset.x,
          y: page.drawingPage.top + Number(control.Y || 0) + offset.y,
          width: page.drawingPage.right - page.drawingPage.left,
          height: page.drawingPage.bottom - page.drawingPage.top,
        };
      };
      const viewportClip = drawing => {
        const rect = drawing.m_oWordControl.m_oEditor?.HtmlElement?.getBoundingClientRect?.();
        return rect ? { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom } : null;
      };
      const pointProjection = (drawingDocument, page, pageIndex, viewportX, viewportY, targetId) => {
        const geometry = pageGeometry(drawingDocument, page);
        const x = (viewportX - geometry.x) / geometry.width;
        const y = (viewportY - geometry.y) / geometry.height;
        if (x < 0 || x > 1 || y < 0 || y > 1) return null;
        const normalizedX = Math.max(0, Math.min(1, x));
        const normalizedY = Math.max(0, Math.min(1, y));
        return {
          target_id: targetId,
          page: pageIndex + 1,
          point: { x: normalizedX, y: normalizedY },
          rect: { x: normalizedX, y: normalizedY, width: 0, height: 0 },
          viewport_rect: { x: viewportX, y: viewportY, width: 0, height: 0 },
          viewport_clip: viewportClip(drawingDocument),
          zoom: drawingDocument.m_oWordControl.m_nZoomValue / 100,
          projection_unavailable: false,
        };
      };
      const projectAnchor = anchor => {
        const region = anchor.region;
        const unavailable = reason => ({ spatial_id: anchor.spatial_id,
          target_id: region.target.target_id, projection_unavailable: true, reason });
        const drawing = editor?.WordControl?.m_oDrawingDocument;
        if (!drawing) return unavailable("编辑器尚未就绪");
        let pageIndex = region.placement.page - 1;
        let x = region.placement.x;
        let y = region.placement.y;
        if (region.target.kind !== "page_region") {
          const object = resolveObject(region.target.target_id);
          if (!object) return unavailable("原锚点目标已失效");
          const attachment = region.attachment;
          const box = attachment && layoutBoxes(object).find(item => item.page_offset === attachment.page_offset);
          if (!box) return unavailable("原锚点缺少可验证的对象附着点，请重新建立");
          pageIndex = box.page;
          const targetPage = drawing.m_arrPages?.[pageIndex];
          if (!targetPage) return unavailable("目标页尚未排版");
          x = (box.x + attachment.x * box.width) / targetPage.width_mm;
          y = (box.y + attachment.y * box.height) / targetPage.height_mm;
        }
        const page = drawing.m_arrPages?.[pageIndex];
        if (!page) return unavailable("原页面已不存在");
        const geometry = pageGeometry(drawing, page);
        const projection = pointProjection(drawing, page, pageIndex,
          geometry.x + x * geometry.width, geometry.y + y * geometry.height, region.target.target_id);
        return projection ? { spatial_id: anchor.spatial_id, ...projection } : unavailable("目标已离开原页面");
      };
      const pointTarget = (logicDocument, pageIndex, x, y) => {
        const drawingInfo = logicDocument.DrawingObjects?.getGraphicInfoUnderCursor?.(pageIndex, x, y) || {};
        if (drawingInfo.objectId) {
          const object = AscCommon.g_oTableId?.Get_ById?.(drawingInfo.objectId) || { Get_Id: () => drawingInfo.objectId };
          const className = `${object?.constructor?.name || ""} ${callValue(object, ["GetClassType"], "")}`.toLowerCase();
          const kind = className.includes("image") || object?.blipFill || object?.getImageUrl ? "image" : "shape";
          return targetObservation(kind, object, null, [{ kind: "drawing", index: indexOf(object), key: String(drawingInfo.objectId) }]);
        }

        const pageFrame = logicDocument.GetPageContentFrame?.(pageIndex);
        const headerFooterZone = pageFrame && (y <= pageFrame.Y || y > pageFrame.YLimit);
        let paragraph = null;
        let headerFooterKind = null;
        if (headerFooterZone) {
          paragraph = logicDocument.HdrFtr?.IsInText?.(x, y, pageIndex) || null;
          if (paragraph) headerFooterKind = y <= (pageFrame.Y + pageFrame.YLimit) / 2 ? "header" : "footer";
        }
        if (!paragraph && logicDocument.Footnotes?.CheckHitInFootnote?.(x, y, pageIndex)) {
          paragraph = logicDocument.Footnotes.IsInText?.(x, y, pageIndex) || null;
        }
        if (!paragraph && logicDocument.Endnotes?.CheckHitInEndnote?.(x, y, pageIndex)) {
          paragraph = logicDocument.Endnotes.IsInText?.(x, y, pageIndex) || null;
        }
        if (!paragraph) paragraph = logicDocument.IsInText?.(x, y, pageIndex) || null;

        let table = logicDocument.IsTableBorder?.(x, y, pageIndex) || null;
        if (!table) {
          const tables = logicDocument.GetAllTablesOnPage?.(pageIndex) || [];
          for (const item of tables) {
            const candidate = item?.Table || item;
            const bounds = candidate?.GetPageBounds?.(item?.Page || 0);
            if (bounds && x >= bounds.Left && x <= bounds.Right && y >= bounds.Top && y <= bounds.Bottom) {
              table = candidate;
              break;
            }
          }
        }

        if (paragraph) {
          const lineage = lineageOf(paragraph);
          const cell = lineage.find(item => typeof item?.GetRow === "function" && typeof item?.GetTable === "function");
          if (cell) {
            const row = cell.GetRow?.();
            const ownerTable = cell.GetTable?.();
            return targetObservation("cell", cell, paragraph, [
              { kind: "table", index: indexOf(ownerTable) },
              { kind: "row", index: indexOf(row) },
              { kind: "cell", index: indexOf(cell) },
            ]);
          }
          if (headerFooterKind) {
            return targetObservation(headerFooterKind, paragraph, paragraph, [
              { kind: headerFooterKind, index: pageIndex },
              { kind: "paragraph", index: indexOf(paragraph) },
            ]);
          }
          return targetObservation("paragraph", paragraph, paragraph, [{ kind: "paragraph", index: indexOf(paragraph) }]);
        }
        if (table) return targetObservation("table", table, null, [{ kind: "table", index: indexOf(table) }]);
        return null;
      };
      const colorChannels = value => [
        parseInt(value.slice(1, 3), 16),
        parseInt(value.slice(3, 5), 16),
        parseInt(value.slice(5, 7), 16),
      ];
      const project = (drawingDocument, page, pageIndex, targetKind) => {
        const drawingPage = page.drawingPage;
        const pageWidth = Math.max(1, drawingPage.right - drawingPage.left);
        const pageHeight = Math.max(1, drawingPage.bottom - drawingPage.top);
        let x = drawingDocument.TargetHtmlElementLeft;
        let y = drawingDocument.TargetHtmlElementTop;
        let width = 2;
        let height = Math.max(2, drawingDocument.m_dTargetSize * drawingDocument.m_oWordControl.m_nZoomValue * AscCommon.g_dKoef_mm_to_pix / 100);
        const selection = page.selectionArray || [];
        const frame = drawingDocument.FrameRect;
        const tableOutline = drawingDocument.TableOutlineDr?.TableOutline;
        if (targetKind === "page_region") {
          x = drawingPage.left;
          y = drawingPage.top;
          width = pageWidth;
          height = pageHeight;
        } else if ((targetKind === "image" || targetKind === "shape") && frame?.IsActive && frame.PageIndex === pageIndex) {
          x = drawingPage.left + frame.Rect.X * pageWidth / page.width_mm;
          y = drawingPage.top + frame.Rect.Y * pageHeight / page.height_mm;
          width = (frame.Rect.R - frame.Rect.X) * pageWidth / page.width_mm;
          height = (frame.Rect.B - frame.Rect.Y) * pageHeight / page.height_mm;
        } else if (targetKind === "table" && tableOutline?.PageNum === pageIndex) {
          x = drawingPage.left + tableOutline.X * pageWidth / page.width_mm;
          y = drawingPage.top + tableOutline.Y * pageHeight / page.height_mm;
          width = tableOutline.W * pageWidth / page.width_mm;
          height = tableOutline.H * pageHeight / page.height_mm;
        } else if (selection.length) {
          const left = Math.min(...selection.map(item => item.x));
          const top = Math.min(...selection.map(item => item.y));
          const right = Math.max(...selection.map(item => item.x + item.w));
          const bottom = Math.max(...selection.map(item => item.y + item.h));
          x = drawingPage.left + left * pageWidth / page.width_mm;
          y = drawingPage.top + top * pageHeight / page.height_mm;
          width = Math.max(2, (right - left) * pageWidth / page.width_mm);
          height = Math.max(2, (bottom - top) * pageHeight / page.height_mm);
        } else if (frame?.IsActive && frame.PageIndex === pageIndex) {
          x = drawingPage.left + frame.Rect.X * pageWidth / page.width_mm;
          y = drawingPage.top + frame.Rect.Y * pageHeight / page.height_mm;
          width = (frame.Rect.R - frame.Rect.X) * pageWidth / page.width_mm;
          height = (frame.Rect.B - frame.Rect.Y) * pageHeight / page.height_mm;
        } else if (tableOutline?.PageNum === pageIndex) {
          x = drawingPage.left + tableOutline.X * pageWidth / page.width_mm;
          y = drawingPage.top + tableOutline.Y * pageHeight / page.height_mm;
          width = tableOutline.W * pageWidth / page.width_mm;
          height = tableOutline.H * pageHeight / page.height_mm;
        }
        return {
          rect: {
            x: Math.max(0, Math.min(1, (x - drawingPage.left) / pageWidth)),
            y: Math.max(0, Math.min(1, (y - drawingPage.top) / pageHeight)),
            width: Math.max(0, Math.min(1, width / pageWidth)),
            height: Math.max(0, Math.min(1, height / pageHeight)),
          },
          viewport_rect: { x, y, width, height },
          zoom: drawingDocument.m_oWordControl.m_nZoomValue / 100,
          projection_unavailable: false,
        };
      };
      const applyExtended = (document, target, paragraph, operation, args, version) => {
        const operationFail = message => ({ changed: false, document_version: version, error: message });
        const kind = command.target_id?.split(":")[0];
        const table = kind === "table" ? target : kind === "cell" ? target.GetRow?.()?.GetTable?.() : null;
        const drawing = ["image", "shape"].includes(kind) ? target : null;
        switch (operation) {
          case "insert_table_row": if (table?.AddRow) table.AddRow(args.index, args.count); else return operationFail("当前目标不支持插入行"); break;
          case "delete_table_row": if (table?.RemoveRow) table.RemoveRow(args.index, args.count); else return operationFail("当前目标不支持删除行"); break;
          case "insert_table_column": if (table?.AddColumn) table.AddColumn(args.index, args.count); else return operationFail("当前目标不支持插入列"); break;
          case "delete_table_column": if (table?.RemoveColumn) table.RemoveColumn(args.index, args.count); else return operationFail("当前目标不支持删除列"); break;
          case "merge_cells": if (table?.MergeCells) table.MergeCells(args.start_row, args.start_column, args.end_row, args.end_column); else return operationFail("当前目标不支持合并单元格"); break;
          case "split_cell": if (table?.SplitCell) table.SplitCell(args.count); else return operationFail("当前目标不支持拆分单元格"); break;
          case "insert_drawing": if (document.AddImage && args.source_url) document.AddImage(args.source_url, args.width, args.height); else return operationFail("缺少图片 URL 或插图 API"); break;
          case "update_drawing": if (drawing?.SetSize) drawing.SetSize(args.width, args.height); else return operationFail("当前目标不支持修改绘图"); break;
          case "delete_drawing": if (drawing?.Delete) drawing.Delete(); else return operationFail("当前目标不支持删除绘图"); break;
          case "add_comment": if (target?.AddComment) target.AddComment(args.text); else return operationFail("当前目标不支持批注"); break;
          case "delete_comment": if (target?.DeleteComment) target.DeleteComment(); else return operationFail("当前目标不支持删除批注"); break;
          case "set_track_changes": if (document.SetTrackRevisions) document.SetTrackRevisions(args.enabled); else return operationFail("当前版本不支持修订 API"); break;
          case "accept_revision": if (document.AcceptRevision) document.AcceptRevision(); else return operationFail("当前版本不支持接受修订 API"); break;
          case "reject_revision": if (document.RejectRevision) document.RejectRevision(); else return operationFail("当前版本不支持拒绝修订 API"); break;
          case "set_watermark": if (document.SetWatermark) document.SetWatermark(args); else return operationFail("当前版本不支持水印 API"); break;
          case "remove_watermark": if (document.RemoveWatermark) document.RemoveWatermark(); else return operationFail("当前版本不支持移除水印 API"); break;
          default: return operationFail(`不支持的扩展操作: ${operation}`);
        }
        return { changed: true, document_version: version + 1, evidence: { history_points: 1 } };
      };
      try {
        if (command.action === "project_anchors") {
          return { changed: false, document_version: command.expected_version,
            evidence: { projections: (command.arguments.anchors || []).map(projectAnchor) } };
        }
        if (command.action === "resolve_anchor_point") {
          const drawing = editor?.WordControl?.m_oDrawingDocument;
          const logicDocument = editor?.WordControl?.m_oLogicDocument;
          const viewportX = Number(command.arguments?.viewport_x);
          const viewportY = Number(command.arguments?.viewport_y);
          if (!drawing || !logicDocument || !Number.isFinite(viewportX) || !Number.isFinite(viewportY)) {
            return fail("编辑器无法解析该页面点");
          }
          const position = drawing.ConvertCoordsFromCursor2?.(viewportX, viewportY);
          const pageIndex = Number(position?.Page);
          const page = drawing.m_arrPages?.[pageIndex];
          if (!position || !Number.isInteger(pageIndex) || pageIndex < 0 || !page?.drawingPage) {
            return fail("点击位置不在可见文档页面内");
          }
          let observation = pointTarget(logicDocument, pageIndex, position.X, position.Y);
          if (!observation) {
            const geometry = pageGeometry(drawing, page);
            const pointX = (viewportX - geometry.x) / geometry.width;
            const pointY = (viewportY - geometry.y) / geometry.height;
            const pointKey = `${pageIndex + 1}:${Math.round(pointX * 1000000)}:${Math.round(pointY * 1000000)}`;
            observation = targetObservation("page_region", null, null, [{ kind: "page", index: pageIndex, key: pointKey }]);
          }
          const projection = pointProjection(drawing, page, pageIndex, viewportX, viewportY, observation.target_id);
          if (!projection) return fail("点击位置不在可见文档页面内");
          if (observation.kind !== "page_region") {
            const raw = AscCommon.g_oTableId?.Get_ById?.(observation.stable_id);
            const object = raw || resolveObject(observation.target_id);
            const box = layoutBoxes(object).find(item => item.page === pageIndex
              && position.X >= item.x && position.X <= item.x + item.width
              && position.Y >= item.y && position.Y <= item.y + item.height);
            if (box) projection.attachment = { page_offset: box.page_offset,
              x: (position.X - box.x) / box.width, y: (position.Y - box.y) / box.height };
          }
          return {
            changed: false,
            document_version: command.expected_version,
            observation,
            projection,
          };
        }
        if (command.action === "project") {
          const drawing = editor?.WordControl?.m_oDrawingDocument;
          const pageIndex = drawing?.m_lTargetPage;
          const page = drawing?.m_arrPages?.[pageIndex];
          if (!drawing || !page || pageIndex < 0) return { target_id: command.target_id, page: 1, projection_unavailable: true };
          const viewport = project(drawing, page, pageIndex, command.target_kind);
          return { target_id: command.target_id, page: pageIndex + 1, ...viewport };
        }
        const document = Api.GetDocument();
        const selection = document.GetRangeBySelect ? document.GetRangeBySelect() : null;
        const requested = command.target_id ? resolveObject(command.target_id) : null;
        if (command.target_id && !requested && ["observe", "edit"].includes(command.action)) return fail("原锚点目标已失效或无法唯一定位");
        const requestedKind = command.target_id?.split(":")[0];
        const paragraph = requested
          ? (["paragraph", "header", "footer"].includes(requestedKind) ? requested : null)
          : document.GetCurrentParagraph?.();
        const table = requested ? (requestedKind === "table" ? requested : null) : document.GetCurrentTable?.();
        const drawing = requested ? (["image", "shape"].includes(requestedKind) ? requested : null) : document.GetCurrentDrawing?.();
        const cell = table?.GetCurrentCell?.() || document.GetCurrentTableCell?.();
        const selectionText = selection?.GetText ? String(selection.GetText()) : "";
        const parent = paragraph?.GetParent?.() || cell?.GetParent?.();
        const contextType = `${String(paragraph?.GetClassType?.() || "")} ${String(parent?.GetClassType?.() || "")}`.toLowerCase();
        const target = requested || drawing || cell || table || (selectionText ? selection : null) || paragraph || document;
        if (command.action === "observe") {
          const text = target?.GetText ? String(target.GetText()) : "";
          const classType = String(target?.GetClassType?.() || "").toLowerCase();
          const kind = drawing ? (classType.includes("image") ? "image" : "shape")
            : contextType.includes("header") ? "header"
              : contextType.includes("footer") ? "footer"
                : cell ? "cell" : table ? "table" : selectionText ? "text_range"
                  : paragraph ? "paragraph" : "page_region";
          const paragraphs = document.GetAllParagraphs?.() || [];
          const tables = document.GetAllTables?.() || [];
          const structurePath = [];
          if (table && tables.includes(table)) structurePath.push({ kind: "table", index: tables.indexOf(table) });
          if (cell) {
            const rowIndex = cell.GetRowIndex?.() ?? cell.GetRow?.()?.GetIndex?.() ?? null;
            const columnIndex = cell.GetColumnIndex?.() ?? cell.GetIndex?.() ?? null;
            structurePath.push({ kind: "cell", row: rowIndex, column: columnIndex });
          }
          if (paragraph && paragraphs.includes(paragraph)) structurePath.push({ kind: "paragraph", index: paragraphs.indexOf(paragraph) });
          if (kind === "header" || kind === "footer") structurePath.unshift({ kind, index: Number(parent?.GetIndex?.() ?? 0) });
          if (kind === "page_region") structurePath.push({ kind: "page", index: Number(editor?.WordControl?.m_oDrawingDocument?.m_lTargetPage ?? 0) });
          const exact = text.slice(0, 1024);
          const stableId = stableIdOf(target);
          const paragraphStyle = paragraph?.GetStyle?.();
          const styleName = typeof paragraphStyle === "string"
            ? paragraphStyle
            : paragraphStyle?.GetName ? String(paragraphStyle.GetName()) : null;
          const targetId = command.target_id || `${kind}:${stableId || hashText(JSON.stringify([structurePath, exact]))}`;
          const drawingDocument = editor?.WordControl?.m_oDrawingDocument;
          const pageIndex = drawingDocument?.m_lTargetPage;
          const page = drawingDocument?.m_arrPages?.[pageIndex];
          const projection = drawingDocument && page && pageIndex >= 0
            ? { target_id: targetId, page: pageIndex + 1, ...project(drawingDocument, page, pageIndex, kind) }
            : { target_id: targetId, page: 1, projection_unavailable: true };
          return {
            changed: false,
            document_version: command.expected_version,
            observation: {
              target_id: targetId,
              kind,
              content: text,
              format: {
                bold: target?.GetBold?.(), italic: target?.GetItalic?.(),
                underline: target?.GetUnderline?.(), style: styleName,
              },
              stable_id: stableId,
              structure_path: structurePath,
              fingerprint: { exact, prefix: exact.slice(0, 128), suffix: exact.slice(-128), style: styleName },
            },
            projection,
          };
        }
        if (command.action === "force_save") {
          if (Api.Save) Api.Save();
          return { changed: false, document_version: command.expected_version, evidence: { force_save_requested: true } };
        }
        if (command.action !== "edit" || !target) return fail("没有可编辑的当前目标");
        if (document.CreateNewHistoryPoint) document.CreateNewHistoryPoint();
        const args = command.arguments || {};
        switch (command.operation) {
          case "replace_text":
            if (!target.SetText) return fail("当前目标不支持替换文字");
            target.SetText(args.text); break;
          case "insert_text": {
            if (!document.CreateParagraph || !target.AddText) return fail("当前目标不支持插入文字");
            target.AddText(args.text); break;
          }
          case "delete_target":
            if (target.Delete) target.Delete(); else if (target.SetText) target.SetText(""); else return fail("当前目标不支持删除");
            break;
          case "set_text_format":
            if (args.bold !== undefined && target.SetBold) target.SetBold(args.bold);
            if (args.italic !== undefined && target.SetItalic) target.SetItalic(args.italic);
            if (args.underline !== undefined && target.SetUnderline) target.SetUnderline(args.underline);
            if (args.strikeout !== undefined && target.SetStrikeout) target.SetStrikeout(args.strikeout);
            if (args.font_name && target.SetFontFamily) target.SetFontFamily(args.font_name);
            if (args.font_size && target.SetFontSize) target.SetFontSize(args.font_size);
            if (args.color && target.SetColor) target.SetColor(...colorChannels(args.color));
            break;
          case "set_paragraph_format":
            if (!paragraph) return fail("当前目标不是段落");
            if (args.alignment && paragraph.SetJc) paragraph.SetJc(args.alignment);
            if (args.line_spacing && paragraph.SetSpacing) paragraph.SetSpacing({ Line: args.line_spacing });
            if (paragraph.SetInd && [args.indent_left, args.indent_right, args.first_line].some(v => v !== undefined)) paragraph.SetInd({ Left: args.indent_left, Right: args.indent_right, FirstLine: args.first_line });
            if (args.page_break_before !== undefined && paragraph.SetPageBreakBefore) paragraph.SetPageBreakBefore(args.page_break_before);
            break;
          case "apply_style":
            if (!paragraph?.SetStyle) return fail("当前段落不支持样式"); paragraph.SetStyle(args.style_name); break;
          case "set_list":
            if (!paragraph?.SetNumbering) return fail("当前段落不支持列表"); paragraph.SetNumbering(args.kind, args.level); break;
          case "set_page_layout":
            if (!document.SetPageSize && !document.SetPageMargins) return fail("当前版本不支持页面设置 API");
            if (args.width && args.height && document.SetPageSize) document.SetPageSize(args.width, args.height);
            if (document.SetPageMargins) document.SetPageMargins(args.margin_left, args.margin_top, args.margin_right, args.margin_bottom);
            break;
          case "set_header_footer": {
            const section = document.GetCurrentSection?.();
            const part = args.kind === "header" ? section?.GetHeader?.(args.odd_even, args.first_page) : section?.GetFooter?.(args.odd_even, args.first_page);
            if (!part?.SetText) return fail("当前版本不支持页眉页脚 API"); part.SetText(args.text); break;
          }
          case "set_watermark":
          case "remove_watermark":
          case "insert_table_row":
          case "delete_table_row":
          case "insert_table_column":
          case "delete_table_column":
          case "merge_cells":
          case "split_cell":
          case "insert_drawing":
          case "update_drawing":
          case "delete_drawing":
          case "add_comment":
          case "delete_comment":
          case "set_track_changes":
          case "accept_revision":
          case "reject_revision":
            return applyExtended(document, target, paragraph, command.operation, args, command.expected_version);
          default: return fail(`不支持的操作: ${command.operation}`);
        }
        return { changed: true, document_version: command.expected_version + 1, evidence: { history_points: 1 } };
      } catch (error) {
        return fail(String(error?.message || error));
      }
    }, false, true, callback);
  }

  async function handle(command) {
    if (command.action === "subscribe_anchors") {
      trackedAnchors = command.arguments.anchors || [];
      command = { ...command, action: "project_anchors" };
    }
    if (command.target_id) activeTargetId = command.target_id;
    const result = await new Promise(resolve => callEditor(command, resolve));
    if (result?.observation?.kind) activeTargetKind = result.observation.kind;
    return result;
  }

  function refreshAnchors() {
    if (projecting || stopped || !trackedAnchors.length) return;
    projecting = true;
    callEditor({ action: "project_anchors", expected_version: documentVersion,
      arguments: { anchors: trackedAnchors } }, result => {
      projecting = false;
      const projections = result?.evidence?.projections;
      if (!projections) return;
      const serialized = JSON.stringify(projections);
      if (serialized === lastProjection) return;
      lastProjection = serialized;
      try { window.top.postMessage({ type: "focus-docx-anchors", session_id: sessionId, projections }, "*"); } catch {}
    });
  }

  async function poll() {
    if (polling || stopped || !bridgeOrigin || !sessionId || !accessToken) return;
    polling = true;
    while (!stopped) {
      try {
        const response = await fetch(endpoint(`/bridge/commands/${encodeURIComponent(sessionId)}/next?timeout=25`));
        if (!response.ok) throw new Error(`poll HTTP ${response.status}`);
        const { command } = await response.json();
        if (!command) continue;
        const result = await handle(command);
        if (result.changed) documentVersion = result.document_version;
        await send(`/bridge/commands/${encodeURIComponent(sessionId)}/${encodeURIComponent(command.command_id)}/result`, {
          ...result,
          target_id: command.target_id,
          operation: command.operation,
        });
      } catch (error) {
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    }
    polling = false;
  }

  function publishTargetPosition() {
    callEditor({ action: "observe", target_id: null, expected_version: documentVersion }, result => {
      const target = result?.observation;
      if (!target) return;
      activeTargetId = target.target_id;
      activeTargetKind = target.kind;
      send(`/bridge/events/${encodeURIComponent(sessionId)}`, {
        event: "selectionChanged", document_version: documentVersion, target, data: {},
      }).catch(() => {});
      try { window.top.postMessage({ type: "focus-docx-target", session_id: sessionId, target }, "*"); } catch {}
      const projection = result?.projection;
      if (projection) {
        send(`/bridge/projections/${encodeURIComponent(sessionId)}`, projection).catch(() => {});
        try { window.top.postMessage({ type: "focus-docx-projection", session_id: sessionId, projection }, "*"); } catch {}
      }
    });
  }

  window.Asc.plugin.init = function () {
    send(`/bridge/events/${encodeURIComponent(sessionId)}`, {
      event: "editorReady", document_version: documentVersion, data: {},
    }).catch(() => {});
    if (typeof window.Asc.plugin.attachEditorEvent === "function") {
      window.Asc.plugin.attachEditorEvent("onDocumentContentChanged", window.Asc.plugin.event_onDocumentContentChanged);
      window.Asc.plugin.attachEditorEvent("onTargetPositionChanged", publishTargetPosition);
    }
    publishTargetPosition();
    projectionTimer = setInterval(refreshAnchors, 150);
    poll();
  };
  window.Asc.plugin.event_onDocumentContentChanged = function () {
    documentVersion += 1;
    send(`/bridge/events/${encodeURIComponent(sessionId)}`, {
      event: "documentChanged", document_version: documentVersion, data: {},
    }).catch(() => {});
  };
  window.Asc.plugin.event_onTargetPositionChanged = publishTargetPosition;
  window.Asc.plugin.event_onSelectionChanged = publishTargetPosition;
  window.addEventListener("beforeunload", () => { stopped = true; clearInterval(projectionTimer); });
})();
