/*
 * Focus spatial projection bridge for ONLYOFFICE Docs Community Edition 9.4.
 * SPDX-License-Identifier: AGPL-3.0-only
 *
 * This source is executed by Asc.plugin.callCommand inside the editor context.
 * It exports existing layout results without changing OOXML or pagination.
 */
function getFocusSpatialProjection(drawing) {
  var pageIndex = drawing.m_lTargetPage;
  var page = drawing.m_arrPages[pageIndex];
  if (!page || pageIndex < 0) return null;
  var drawingPage = page.drawingPage;
  var pageWidth = Math.max(1, drawingPage.right - drawingPage.left);
  var pageHeight = Math.max(1, drawingPage.bottom - drawingPage.top);
  var x = drawing.TargetHtmlElementLeft;
  var y = drawing.TargetHtmlElementTop;
  var width = 2;
  var height = Math.max(2, drawing.m_dTargetSize * drawing.m_oWordControl.m_nZoomValue * AscCommon.g_dKoef_mm_to_pix / 100);
  var selection = page.selectionArray || [];
  if (selection.length) {
    var left = Math.min.apply(null, selection.map(function (item) { return item.x; }));
    var top = Math.min.apply(null, selection.map(function (item) { return item.y; }));
    var right = Math.max.apply(null, selection.map(function (item) { return item.x + item.w; }));
    var bottom = Math.max.apply(null, selection.map(function (item) { return item.y + item.h; }));
    x = drawingPage.left + left * pageWidth / page.width_mm;
    y = drawingPage.top + top * pageHeight / page.height_mm;
    width = Math.max(2, (right - left) * pageWidth / page.width_mm);
    height = Math.max(2, (bottom - top) * pageHeight / page.height_mm);
  } else if (drawing.FrameRect && drawing.FrameRect.IsActive && drawing.FrameRect.PageIndex === pageIndex) {
    var rect = drawing.FrameRect.Rect;
    x = drawingPage.left + rect.X * pageWidth / page.width_mm;
    y = drawingPage.top + rect.Y * pageHeight / page.height_mm;
    width = (rect.R - rect.X) * pageWidth / page.width_mm;
    height = (rect.B - rect.Y) * pageHeight / page.height_mm;
  } else if (drawing.TableOutlineDr && drawing.TableOutlineDr.TableOutline && drawing.TableOutlineDr.TableOutline.PageNum === pageIndex) {
    var table = drawing.TableOutlineDr.TableOutline;
    x = drawingPage.left + table.X * pageWidth / page.width_mm;
    y = drawingPage.top + table.Y * pageHeight / page.height_mm;
    width = table.W * pageWidth / page.width_mm;
    height = table.H * pageHeight / page.height_mm;
  }
  return {
    page: pageIndex + 1,
    rect: {
      x: Math.max(0, Math.min(1, (x - drawingPage.left) / pageWidth)),
      y: Math.max(0, Math.min(1, (y - drawingPage.top) / pageHeight)),
      width: Math.max(0, Math.min(1, width / pageWidth)),
      height: Math.max(0, Math.min(1, height / pageHeight))
    },
    viewport_rect: { x: x, y: y, width: width, height: height },
    zoom: drawing.m_oWordControl.m_nZoomValue / 100
  };
}
