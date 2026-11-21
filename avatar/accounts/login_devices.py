"""新设备登录提醒；随机浏览器标识按账户散列保存，IP 变化本身不触发重复提醒。"""
import hashlib
import ipaddress
import re
from avatar.core.db import dump,event,now


# User-Agent 只用于展示环境，不参与认证；不信任客户端提供的转发 IP 头。
def device_context(request,identifier):
    agent=request.headers.get('user-agent','')[:1024]
    os='未知系统'
    for pattern,label in [(r'Android[ /]([\d.]+)','Android'),(r'(?:iPhone OS|CPU OS) ([\d_]+)','iOS'),(r'Mac OS X[ /]([\d_]+)','macOS'),(r'Windows NT ([\d.]+)','Windows')]:
        match=re.search(pattern,agent,re.I)
        if match:
            version=match[1].replace('_','.')
            os='Windows 10/11' if label=='Windows' and version=='10.0' else label+' '+version
            break
    else:
        if 'Linux' in agent:os='Linux'
    browser='未知浏览器';webview='未知引擎'
    for token,label in [('Edg','Edge'),('OPR','Opera'),('Firefox','Firefox'),('Chrome','Chrome'),('Version','Safari')]:
        match=re.search(re.escape(token)+r'/([\d.]+)',agent)
        if match:browser=label+' '+match[1];break
    for token,label in [('Chrome','Chromium'),('AppleWebKit','WebKit'),('Gecko','Gecko')]:
        match=re.search(re.escape(token)+r'/([\d.]+)',agent)
        if match:webview=label+' '+match[1];break
    if re.search(r';\s*wv\)',agent):webview='Android WebView · '+webview
    elif 'MicroMessenger/' in agent:webview='微信 WebView · '+webview
    elif 'Electron/' in agent:webview='Electron · '+webview
    try:ip=str(ipaddress.ip_address(request.client.host))
    except (ValueError,AttributeError):ip='未知'
    return {'hash':hashlib.sha256(identifier.encode()).hexdigest(),'os':os,'browser':browser,'webview':webview,'ip':ip}


# 与已验签账号建会话共用事务；首次识别该浏览器才提醒，重试/并发登录由唯一键去重。
def record_device(db,user_id,device):
    if not device:return
    stamp=now()
    inserted=db.execute('''INSERT OR IGNORE INTO login_devices
      (user_id,device_hash,os,browser,webview,ip,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?)''',
      (user_id,device['hash'],device['os'],device['browser'],device['webview'],device['ip'],stamp,stamp)).rowcount
    db.execute('UPDATE login_devices SET os=?,browser=?,webview=?,ip=?,last_seen=? WHERE user_id=? AND device_hash=?',
      (device['os'],device['browser'],device['webview'],device['ip'],stamp,user_id,device['hash']))
    if inserted:event(db,user_id,'keycloak','统一认证登录',dump({key:device[key] for key in ('os','browser','webview','ip')}))
