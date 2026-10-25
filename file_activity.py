#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_activity —— 用 mtime 记录一个目录里哪些文件"还活着"

为什么不用 atime（最后读取时间）：
  很多环境里 atime 是被实际关闭的 —— 移动端为省写入、容器和网络挂载为省 IO，
  常挂成 relatime 甚至 noatime。实测：读一个文件前后 atime 完全不变，
  这时 atime 只等于"创建时间"，没有任何参考价值。
  所以这个工具只用 mtime（最后修改时间）+ size。

用法：
  python3 file_activity.py snapshot [备注]    存一次基线快照
  python3 file_activity.py diff               跟上次快照比：新增 / 修改 / 消失
  python3 file_activity.py cold [天数]        列出 N 天没改过的文件（默认 30），按大小排
  python3 file_activity.py history            列出所有快照

命令也认中文：快照 / 变化 / 冷件 / 历史

参数：
  --root DIR   要扫描的目录（默认当前目录）
  --db PATH    数据库路径（默认 <root>/.file_activity.sqlite）
  --all        把 .git / node_modules 这类默认跳过的目录也算进来

★ 冷件不等于该删：从 tar/zip 解包出来的老素材会带着几十年前的原始时间戳，
  判断"这个文件过时了"要结合"还有没有别处引用 / 还在不在用"。
"""

import os
import sys
import time
import sqlite3
import collections

DEFAULT_SKIP = {'.git', '.svn', '.hg', 'node_modules', '.venv', 'venv',
                '__pycache__', '.npm-cache', '.cache', 'dist', 'build',
                '.idea', '.vscode'}

ALIAS = {
    'snapshot': 'snapshot', '快照': 'snapshot',
    'diff': 'diff', '变化': 'diff',
    'cold': 'cold', '冷件': 'cold',
    'history': 'history', '历史': 'history',
}

DB_NAME = '.file_activity.sqlite'


def bj(ts):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ts)) if ts else '-'


def days_ago(ts):
    return (time.time() - ts) / 86400.0


def human(n):
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return '%.1f%s' % (n, u)
        n /= 1024.0
    return '%d' % n


def walk(root, all_=False, skip=DEFAULT_SKIP):
    """产出 (相对路径, 大小, mtime)"""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if not all_:
            dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            if f == DB_NAME:
                continue
            p = os.path.join(dirpath, f)
            rp = os.path.join(rel, f) if rel != '.' else f
            try:
                st = os.lstat(p)
            except OSError:
                continue
            yield rp, st.st_size, int(st.st_mtime)


def db(path):
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS snap(id INTEGER PRIMARY KEY, ts INTEGER, note TEXT, n INTEGER, total INTEGER);
      CREATE TABLE IF NOT EXISTS f(snap INTEGER, path TEXT, size INTEGER, mtime INTEGER);
      CREATE INDEX IF NOT EXISTS ix_f_snap ON f(snap);
      CREATE INDEX IF NOT EXISTS ix_f_path ON f(snap, path);
    """)
    return c


def do_snapshot(conn, root, note, all_):
    t0 = time.time()
    n = tot = 0
    snap = conn.execute("INSERT INTO snap(ts,note,n,total) VALUES(?,?,0,0)",
                        (int(time.time()), note)).lastrowid
    buf = []
    for p, s, m in walk(root, all_):
        buf.append((snap, p, s, m))
        n += 1
        tot += s
        if len(buf) >= 20000:
            conn.executemany("INSERT INTO f VALUES(?,?,?,?)", buf)
            buf = []
    if buf:
        conn.executemany("INSERT INTO f VALUES(?,?,?,?)", buf)
    conn.execute("UPDATE snap SET n=?, total=? WHERE id=?", (n, tot, snap))
    conn.commit()
    print("快照 #%d 存好：%d 个文件 / %.2f GB  (%.1fs)  备注：%s"
          % (snap, n, tot / 1024 ** 3, time.time() - t0, note or '无'))


def do_history(conn):
    rows = conn.execute("SELECT id,ts,note,n,total FROM snap ORDER BY id").fetchall()
    if not rows:
        print("还没有任何快照，先跑：snapshot")
        return
    print("%-5s %-17s %9s %10s  %s" % ("#", "时间", "文件数", "大小", "备注"))
    for i, ts, note, n, tot in rows:
        print("%-5d %-17s %9d %10s  %s" % (i, bj(ts), n, human(tot), note or ''))


def do_diff(conn):
    ids = [r[0] for r in conn.execute("SELECT id FROM snap ORDER BY id DESC LIMIT 2")]
    if len(ids) < 2:
        print("只有 %d 次快照，至少要两次才能比。再跑一次：snapshot" % len(ids))
        return
    new_id, old_id = ids[0], ids[1]
    old = {p: (s, m) for p, s, m in
           conn.execute("SELECT path,size,mtime FROM f WHERE snap=?", (old_id,))}
    new = {p: (s, m) for p, s, m in
           conn.execute("SELECT path,size,mtime FROM f WHERE snap=?", (new_id,))}
    added = [p for p in new if p not in old]
    gone = [p for p in old if p not in new]
    mod = [p for p in new if p in old and new[p] != old[p]]
    t0 = conn.execute("SELECT ts,note FROM snap WHERE id=?", (old_id,)).fetchone()
    t1 = conn.execute("SELECT ts,note FROM snap WHERE id=?", (new_id,)).fetchone()
    print("对比： #%d (%s %s)  →  #%d (%s %s)\n"
          % (old_id, bj(t0[0]), t0[1] or '', new_id, bj(t1[0]), t1[1] or ''))

    def by_top(lst):
        cnt = collections.Counter()
        sz = collections.Counter()
        for p in lst:
            t = p.split(os.sep)[0]
            cnt[t] += 1
            sz[t] += new.get(p, old.get(p, (0, 0)))[0]
        return "  ".join("%s(%d个/%s)" % (k, cnt[k], human(sz[k])) for k, _ in cnt.most_common(6))

    print("新增 %d 个   %s" % (len(added), by_top(added)))
    print("修改 %d 个   %s" % (len(mod), by_top(mod)))
    print("消失 %d 个   %s" % (len(gone), by_top(gone)))
    for name, lst in (('新增', added), ('修改', mod)):
        if lst:
            print("\n-- %s 最大的 15 个 --" % name)
            for p in sorted(lst, key=lambda x: -new.get(x, old.get(x, (0, 0)))[0])[:15]:
                print("   %-10s %s" % (human(new.get(p, old.get(p, (0, 0)))[0]), p))


def do_cold(root, days):
    lim = time.time() - days * 86400
    rows = [(p, s, m) for p, s, m in walk(root) if m < lim]
    if not rows:
        print("%d 天内没有冷件（所有文件都在 %d 天里动过）" % (days, days))
        return
    tot = sum(r[1] for r in rows)
    cnt = collections.Counter()
    sz = collections.Counter()
    for p, s, _ in rows:
        t = p.split(os.sep)[0]
        cnt[t] += 1
        sz[t] += s
    print("%d 天没动过的：%d 个文件 / %s\n" % (days, len(rows), human(tot)))
    print("%-16s %8s %10s" % ("目录", "文件数", "大小"))
    for t, _ in sz.most_common():
        print("%-16s %8d %10s" % (t, cnt[t], human(sz[t])))
    print("\n-- 最大的 25 个 --")
    for p, s, m in sorted(rows, key=lambda x: -x[1])[:25]:
        print("   %-10s %6.1f天前  %s" % (human(s), days_ago(m), p))


def main():
    argv = sys.argv[1:]
    root = os.getcwd()
    dbpath = None
    all_ = '--all' in argv or '--全部' in argv
    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('--all', '--全部'):
            pass
        elif a == '--root' and i + 1 < len(argv):
            i += 1
            root = os.path.abspath(argv[i])
        elif a == '--db' and i + 1 < len(argv):
            i += 1
            dbpath = os.path.abspath(argv[i])
        elif a in ('-h', '--help'):
            print(__doc__)
            return
        else:
            args.append(a)
        i += 1

    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print("目录不存在：%s" % root)
        return
    dbpath = dbpath or os.path.join(root, DB_NAME)

    cmd = ALIAS.get(args[0], 'history') if args else 'history'
    conn = db(dbpath)
    try:
        if cmd == 'snapshot':
            do_snapshot(conn, root, ' '.join(args[1:]), all_)
        elif cmd == 'diff':
            do_diff(conn)
        elif cmd == 'history':
            do_history(conn)
        elif cmd == 'cold':
            do_cold(root, int(args[1]) if len(args) > 1 else 30)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
