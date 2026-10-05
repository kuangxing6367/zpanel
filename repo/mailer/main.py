# -*- coding: utf-8 -*-
"""mailer —— SMTP 发信（机制包）。

只做一件事：把一封邮件通过 SMTP 投出去。**不含任何业务语义** ——
谁发、发给谁、发什么内容，全部由调用方决定（告警、审计、报表都用同一份实现）。

稳定 API 表面：
    send(cfg, subject, text, html='', to=None) -> dict
        cfg = {
          'host': 'smtp.qq.com', 'port': 465,
          'user': 'a@qq.com', 'password': '授权码',
          'tls': 'ssl' | 'starttls' | 'none',   # 默认按端口推断：465=ssl, 587=starttls
          'sender': 'a@qq.com',                 # 缺省用 user
          'from_name': 'ZPanel',
          'timeout': 12,
        }
    返回值 {'ok': True, 'to': [...], 'server': 'host:port', 'elapsed_ms': int}
    失败抛 MailError（消息里带上游原文，便于排查）。

设计约定：
- **纯标准库**（smtplib/email），不引入第三方依赖 —— 内核侧零新增负担；
- 收件人支持 str（逗号/分号分隔）或 list；
- 不自己读配置：配置由调用方（扩展 / 服务层）传入并落库，本包不碰存储。
"""
from __future__ import annotations

import smtplib
import ssl
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid


class MailError(RuntimeError):
    """发信失败（配置错误 / 连接失败 / 上游拒收）。"""


def parse_recipients(to) -> list:
    """收件人归一化：str（逗号/分号/空白分隔）或 list -> 去空去重的列表。"""
    if not to:
        return []
    if isinstance(to, str):
        raw = to.replace(';', ',').replace('\n', ',').split(',')
    else:
        raw = list(to)
    out = []
    for x in raw:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def _tls_mode(cfg: dict) -> str:
    mode = str(cfg.get('tls') or '').strip().lower()
    if mode in ('ssl', 'starttls', 'none'):
        return mode
    port = int(cfg.get('port') or 0)
    if port == 465:
        return 'ssl'
    if port in (587, 25, 2525):
        return 'starttls' if port != 25 else 'none'
    return 'ssl' if port == 465 else 'none'


def send(cfg: dict, subject: str, text: str, html: str = '', to=None) -> dict:
    """发送一封邮件。成功返回投递摘要，失败抛 MailError。"""
    host = str(cfg.get('host') or '').strip()
    port = int(cfg.get('port') or 0)
    user = str(cfg.get('user') or '').strip()
    password = str(cfg.get('password') or '')
    sender = (str(cfg.get('sender') or '').strip() or user)
    from_name = str(cfg.get('from_name') or 'ZPanel').strip()
    timeout = float(cfg.get('timeout') or 12)

    if not host:
        raise MailError('缺少 SMTP 服务器地址（host）')
    if not port:
        port = 465 if _tls_mode(cfg) == 'ssl' else 587
    if not sender:
        raise MailError('缺少发件人（sender 或 user）')

    rcpts = parse_recipients(to)
    if not rcpts:
        raise MailError('缺少收件人（to）')

    if html:
        msg = MIMEMultipart('alternative')
        msg.attach(MIMEText(text or '', 'plain', 'utf-8'))
        msg.attach(MIMEText(html, 'html', 'utf-8'))
    else:
        msg = MIMEText(text or '', 'plain', 'utf-8')
    msg['Subject'] = Header(subject or '(无主题)', 'utf-8')
    msg['From'] = formataddr((str(Header(from_name, 'utf-8')), sender)) if from_name else sender
    msg['To'] = ', '.join(rcpts)
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid()

    mode = _tls_mode(cfg)
    ctx = ssl.create_default_context()
    t0 = time.time()
    try:
        if mode == 'ssl':
            srv = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            srv = smtplib.SMTP(host, port, timeout=timeout)
        with srv:
            srv.ehlo()
            if mode == 'starttls':
                srv.starttls(context=ctx)
                srv.ehlo()
            if user:
                srv.login(user, password)
            srv.sendmail(sender, rcpts, msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(f'SMTP 认证失败：{e}') from e
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        raise MailError(f'{type(e).__name__}: {e}') from e

    return {'ok': True, 'to': rcpts, 'server': f'{host}:{port}',
            'tls': mode, 'elapsed_ms': int((time.time() - t0) * 1000)}
