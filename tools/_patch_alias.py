p = 'qq_autoreply.py'
s = open(p, encoding='utf-8').read()
orig = s

# ① 抽出「记别名」的公用方法，放在 _friend_ids 之前
s = s.replace('''    def _friend_ids(self, refresh=False, ttl=600):''',
'''    def _remember_friend_aliases(self, friends):
        """把好友的备注 / 昵称记进名字索引。

        为什么要单独抽出来：scan_new_friends 自己拉了一次 get_friend_list，
        以前只更新了 id 缓存、**没记别名**——于是主人问「你还记得姚明希吗」
        （那是他给的备注）时认不出来，索引里只有昵称「　　　　　」。
        两处各自写一遍就会再漏一次，所以收成一个方法，谁拿到列表谁调它。
        """
        for f in friends or []:
            try:
                uid = int(f.get("user_id") or 0)
            except (TypeError, ValueError):
                continue
            if not uid:
                continue
            try:
                self.mem.add_alias(f.get("remark"), uid)
                self.mem.add_alias(f.get("nickname"), uid)
            except Exception:
                pass

    def _friend_ids(self, refresh=False, ttl=600):''')

# ② _friend_ids 里改成调用公用方法
s = s.replace('''            ids.add(uid)
            # 顺带记别名：主人问「你还记得康程锦吗」用的是**他给的备注**，
            # 而对方本人叫「Changin丶Lin」——两个都得认。
            try:
                self.mem.add_alias(f.get("remark"), uid)
                self.mem.add_alias(f.get("nickname"), uid)
            except Exception:
                pass''',
'''            ids.add(uid)''')
s = s.replace('''        self._friend_cache = (now, ids)
        return ids''',
'''        # 记别名：主人问「你还记得康程锦吗」用的是**他给的备注**，
        # 而对方本人叫「Changin丶Lin」——两个都得认。
        self._remember_friend_aliases(data)
        self._friend_cache = (now, ids)
        return ids''')

# ③ scan_new_friends 拿到列表后也记别名（这是漏掉的主因）
s = s.replace('''        wl = set(int(x) for x in self.cfg.get("allowed_private") or [])''',
'''        # 先记别名：这个函数自己拉了列表，不能只更新 id 缓存就完事
        self._remember_friend_aliases(friends)
        wl = set(int(x) for x in self.cfg.get("allowed_private") or [])''')

assert s != orig
tmp = p + '.new'
with open(tmp, 'w', encoding='utf-8', newline='') as f:
    f.write(s)
import os
os.replace(tmp, p)
print('ok')
