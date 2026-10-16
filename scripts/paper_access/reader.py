"""Save the exact existing Edge reader once through its native controller."""
import base64
import json
import re
import time
from pathlib import Path

SAVE = r"""(async () => {
  const controller = window.viewer.pluginController_;
  const result = await controller.save(true);
  if (!result?.dataToSave) return null;
  const chunks = [new Uint8Array(result.dataToSave)];
  if (typeof loadTimeData !== 'undefined' && loadTimeData.getBoolean('pdfSaveAsChunkBufferEnabled')) {
    while (true) {
      const block = await controller.getNextSaveBlock(result.token);
      if (!block) break;
      chunks.push(new Uint8Array(block));
    }
  }
  let binary = '';
  for (const chunk of chunks) for (let i = 0; i < chunk.length; i += 32768)
    binary += String.fromCharCode.apply(null, chunk.subarray(i, i + 32768));
  controller.cleanUpSaveTokenEntry(result.token);
  return btoa(binary);
})()"""


def evaluate_target(control, page, target_id, expression, timeout=20):
    """Use public CDP transport for a webview absent from Playwright pages."""
    session_id = control.send('Target.attachToTarget', {'targetId': target_id, 'flatten': False})['sessionId']
    replies = []
    def received(event):
        if event.get('sessionId') == session_id:
            message = json.loads(event['message'])
            if message.get('id') == 1:
                replies.append(message)
    control.on('Target.receivedMessageFromTarget', received)
    try:
        control.send('Target.sendMessageToTarget', {'sessionId': session_id, 'message': json.dumps({
            'id': 1, 'method': 'Runtime.evaluate', 'params': {
                'expression': expression, 'awaitPromise': True, 'returnByValue': True}})})
        deadline = time.monotonic() + timeout
        while not replies and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        if not replies or 'error' in replies[0] or 'exceptionDetails' in replies[0].get('result', {}):
            return None
        return replies[0].get('result', {}).get('result', {}).get('value')
    finally:
        control.remove_listener('Target.receivedMessageFromTarget', received)
        control.send('Target.detachFromTarget', {'sessionId': session_id})


def save_existing_edge_reader(control, page, paper, state, checkpoint):
    if state.get('reader_save_submitted'):
        return None
    from .routes import matches_article
    for target in control.send('Target.getTargets')['targetInfos']:
        if target.get('type') != 'webview' or not target.get('url', '').startswith(
                'chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/edge_pdf/'):
            continue
        observed = evaluate_target(control, page, target['targetId'],
            "({url:window.viewer?.originalUrl_ || '', ready:typeof window.viewer?.pluginController_?.save === 'function'})", 3)
        if not isinstance(observed, dict) or not observed.get('ready'):
            continue
        if observed.get('url') != page.url or not matches_article(observed['url'], paper):
            continue
        # Persist before invoking: a timeout or restart must never repeat Save.
        state.update(reader_save_submitted=True, state='reader_save_submitted')
        checkpoint()
        result = evaluate_target(control, page, target['targetId'], SAVE)
        return base64.b64decode(result, validate=True) if isinstance(result, str) else None
    return None


ITEMS = r"""() => {
  const items=[];
  function walk(root) {for(const e of root.querySelectorAll('*')) {
    if(e.tagName==='DOWNLOAD-ITEM') {
      let path='';try {path=new URL(e.icon||e.getAttribute('icon')||'').searchParams.get('path')||'';}catch{}
      items.push({id:e.id,state:e.state||e.getAttribute('state'),details:e.details||'',path,
        canResume:!!e.shadowRoot?.querySelector('[data-action="resume"]')});
    }else if(e.shadowRoot) walk(e.shadowRoot);
  }}walk(document);return items;
}"""


def follow_native_download(browser, state, staging, checkpoint, paper):
    """Read or resume the exact GUID; do not create a second download."""
    if not state.get('guid') or not browser.external_browser or browser.cfg.get('browser_channel') != 'msedge':
        return None
    page = next((p for p in browser.context.pages if p.url.startswith('edge://downloads/all')), None)
    if page is None:
        page = browser.context.new_page()
        page.goto('edge://downloads/all')
        page.wait_for_timeout(500)
    # Edge's full downloads page is a snapshot on some versions.
    if time.monotonic() - getattr(browser, '_downloads_refreshed', 0) > 3:
        browser._downloads_refreshed = time.monotonic()
        page.reload()
        page.wait_for_timeout(500)
    pages = [page] + [p for p in browser.context.pages if p.url.startswith('edge://downloads-hub/')]
    expected = (staging / state['guid']).resolve()
    for surface in pages:
        items = surface.evaluate(ITEMS)
        item = next((i for i in items if i.get('path') and Path(i['path']).resolve() == expected), None)
        if not item:
            continue
        state['download_state'] = item['state']
        checkpoint()
        if item['state'] == 'complete' and expected.is_file():
            return expected.read_bytes()
        if (item['state'] != 'interrupted' or not item['canResume'] or state.get('resumes', 0)
                or not re.search(r'网络问题|网络错误|网络超时|网络连接|Network (?:error|failed|timeout)|Disconnected', item['details'], re.I)):
            continue
        from .storage import Store
        from urllib.parse import urlsplit
        store = Store(browser.cfg['data_dir'])
        try:
            if store.cooldown(urlsplit(paper['article_url']).hostname):
                return None
        finally:
            store.close()
        state['resumes'] = 1
        checkpoint()
        surface.evaluate(r"""id => {
          let found=null,list=null;
          function walk(root) {for(const e of root.querySelectorAll('*')) {
            if(e.tagName==='DOWNLOADS-LIST') list=e;
            if(e.tagName==='DOWNLOAD-ITEM' && e.id===id) found=e;
            else if(e.shadowRoot) walk(e.shadowRoot);
          }}walk(document);
          if(found?.state!=='interrupted' || !found.shadowRoot?.querySelector('[data-action="resume"]')
              || typeof list?.onDownloadItemEvent!=='function') return false;
          list.onDownloadItemEvent({detail:{type:'resume-click',id}});return true;
        }""", item['id'])
    return None
