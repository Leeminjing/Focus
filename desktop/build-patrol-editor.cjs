/* 本文件提供会话 Patrol 文本编辑器的可复现浏览器构建入口。
 * 输入为 npm 锁定的 CodeMirror/esbuild 与 patrol-json-editor.mjs；输出为离线 IIFE bundle 和第三方许可文件。
 * 工作流在 desktop 内执行 npm run build:patrol-editor，应用直接载入 bundle，无 CDN 或运行时网络依赖。
 * 示例：npm ci && npm run build:patrol-editor。
 */
const path = require('node:path');
const fs = require('node:fs');
require('esbuild').buildSync({
  entryPoints:[path.join(__dirname,'patrol-json-editor.mjs')],
  outfile:path.join(__dirname,'patrol-json-editor.bundle.js'),
  bundle:true, format:'iife', globalName:'FocusPatrolJsonEditor', platform:'browser', target:'chrome150',
  minify:true, legalComments:'external',
  banner:{js:'/* 本文件对外提供 FocusPatrolJsonEditor.createJsonEditor。输入为 label，输出为 element/value/selection/readOnly 编辑端口；工作流为完整文本状态与可见区域排版。示例：createJsonEditor({label:"JSON"})。由 npm run build:patrol-editor 生成，源码见 patrol-json-editor.mjs。 */'},
});
const dependencies = ['@codemirror/state','@codemirror/view','@codemirror/commands','@codemirror/language','@lezer/common','@lezer/highlight','@lezer/lr','@marijn/find-cluster-break','style-mod','w3c-keyname','crelt'];
fs.writeFileSync(path.join(__dirname,'patrol-json-editor.LICENSE.txt'), dependencies.map(name => {
  const license = fs.readFileSync(path.join(__dirname,'node_modules',name,'LICENSE'),'utf8');
  return `${name}\n${license}`;
}).join('\n\n'));
