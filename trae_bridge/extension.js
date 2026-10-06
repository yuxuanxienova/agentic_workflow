// 本机 unix socket ~/.trae-cn/agentic-bridge.sock：收一行 JSON {"text": "..."}，
// 送进当前 Trae agent 会话（内部命令 feishu.bridge.sendAndWaitResponse），回 {"ok":true,"text":回复}
const vscode = require('vscode'), net = require('net'), fs = require('fs'), os = require('os'), path = require('path');
const SOCK = path.join(os.homedir(), '.trae-cn', 'agentic-bridge.sock');
let server, busy = false;
function activate(ctx) {
  try { fs.unlinkSync(SOCK); } catch {}
  server = net.createServer(c => {
    let buf = '';
    c.on('data', async d => {
      buf += d; if (!buf.includes('\n')) return;
      let req; try { req = JSON.parse(buf.split('\n')[0]); } catch (e) { return c.end(JSON.stringify({ok:false,error:'bad json'})+'\n'); }
      if (req.op === 'ping') return c.end(JSON.stringify({ok:true,busy,workspace:(vscode.workspace.workspaceFolders||[]).map(f=>f.uri.fsPath)})+'\n');
      if (req.op === 'exec') { // 调试：执行任意命令，结果尽量 JSON 化
        try { const r = await vscode.commands.executeCommand(req.cmd, ...(req.args||[]));
          let out; try { out = JSON.parse(JSON.stringify(r===undefined?null:r)); } catch { out = String(r); }
          return c.end(JSON.stringify({ok:true,result:out})+'\n'); }
        catch (e) { return c.end(JSON.stringify({ok:false,error:String(e && e.message || e)})+'\n'); }
      }
      if (busy && !req.force) return c.end(JSON.stringify({ok:false,busy:true})+'\n');
      busy = true;
      try {
        const cmd = req.wait === false ? 'im.bridge.handleIncomingMessage' : 'feishu.bridge.sendAndWaitResponse';
        const r = req.wait === false
          ? await vscode.commands.executeCommand(cmd, {channel:'feishu', text:req.text, messageId:'bus-'+Date.now(), sentAt:Date.now()})
          : await vscode.commands.executeCommand(cmd, req.text, []);
        c.end(JSON.stringify({ok:true,text:r && r.text})+'\n');
      } catch (e) { c.end(JSON.stringify({ok:false,error:String(e && e.message || e)})+'\n'); }
      finally { busy = false; }
    });
  });
  server.listen(SOCK);
  ctx.subscriptions.push({dispose(){ server.close(); try{fs.unlinkSync(SOCK);}catch{} }});
}
function deactivate() { if (server) server.close(); }
module.exports = { activate, deactivate };
