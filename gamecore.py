# -*- coding: utf-8 -*-
"""
棋牌游戏引擎 (本地版 server.py 与云端版 deploy/app.py 共用)
包含: 欢乐斗牛(DouniuRoom) 与 炸金花(ZhajinhuaRoom)
"""
import asyncio
import itertools
import random

BASE_SCORE = 10        # 底分
START_MONEY = 1000     # 初始金币
MAX_PLAYERS = 5        # 一桌最多人数

GRAB_TIME = 12         # 斗牛抢庄倒计时
TURN_TIME = 15         # 行动倒计时(每人)
REVEAL_TIME = 4        # 亮牌展示时间
SETTLE_TIME = 20       # 结算展示时间(可手动提前开始下一局)
MAX_STAKE_MULT = 8     # 单注上限(底分的倍数)

GAMES = ("douniu", "zjh")
GAME_NAMES = {"douniu": "欢乐斗牛", "zjh": "炸金花"}


# ================================================================ 牌型计算
def points(rank: int) -> int:
    """斗牛点数: J/Q/K/10 记 10 点, A 记 1 点"""
    return 10 if rank >= 10 else rank


def calc_cards(ranks):
    """斗牛 5 张牌牌型: {mult, name, combo}"""
    ranks = list(ranks)
    if all(r <= 5 for r in ranks) and sum(points(r) for r in ranks) <= 10:
        return {"mult": 6, "name": "五小牛", "combo": None}
    if all(r >= 11 for r in ranks):
        return {"mult": 6, "name": "五花牛", "combo": None}
    for r in set(ranks):
        if ranks.count(r) == 4:
            return {"mult": 5, "name": "炸弹", "combo": None}
    face = sum(1 for r in ranks if r >= 11)
    if face == 4 and all(points(r) == 10 for r in ranks):
        return {"mult": 4, "name": "四花牛", "combo": None}
    for combo in itertools.combinations(range(5), 3):
        if sum(points(ranks[i]) for i in combo) % 10 == 0:
            rest = sum(points(r) for r in ranks) - \
                sum(points(ranks[i]) for i in combo)
            niu = rest % 10
            if niu == 0:
                return {"mult": 3, "name": "牛牛", "combo": list(combo)}
            return {"mult": 2 if niu >= 7 else 1,
                    "name": f"牛{niu}", "combo": list(combo)}
    return {"mult": 1, "name": "无牛", "combo": None}


# ---------------- 炸金花牌型 ----------------
def zjh_straight_key(rs):
    """顺子关键值; A 可作 1 (A23 最小顺) 或 14 (QKA 最大顺); 非顺返回 None"""
    s = sorted(set(rs))
    if len(s) != 3:
        return None
    if s[2] - s[0] == 2:
        return s[2]
    if s == [1, 12, 13]:
        return 14
    if s == [1, 2, 3]:
        return 3
    return None


def calc_zjh(cards):
    """
    炸金花 3 张牌牌型, cards = [(rank, suit), ...]
    返回 {"name", "value"(比大小元组, 越大越强)}
    豹子 > 顺金 > 金花 > 顺子 > 对子 > 单张
    """
    rs = sorted((c[0] for c in cards), reverse=True)
    flush = len({c[1] for c in cards}) == 1
    sk = zjh_straight_key([c[0] for c in cards])
    if rs[0] == rs[2]:
        return {"name": "豹子", "value": (6, rs[0])}
    if flush and sk:
        return {"name": "顺金", "value": (5, sk)}
    if flush:
        return {"name": "金花", "value": (4, *rs)}
    if sk:
        return {"name": "顺子", "value": (3, sk)}
    if rs[0] == rs[1] or rs[1] == rs[2]:
        pr = rs[0] if rs[0] == rs[1] else rs[1]
        kick = rs[2] if rs[0] == rs[1] else rs[0]
        return {"name": "对子", "value": (2, pr, kick)}
    return {"name": "单张", "value": (1, *rs)}


def zjh_beat(a, b):
    """比较两手牌: a > b 返回 True; value 相同时比最大花色(黑桃最大)"""
    if a["value"] != b["value"]:
        return a["value"] > b["value"]
    return max(s for _, s in a["cards"]) > max(s for _, s in b["cards"])


# ================================================================ 玩家
class Player:
    _next_id = 1

    def __init__(self, conn, name):
        self.id = Player._next_id
        Player._next_id += 1
        self.conn = conn                # 需具备 async send_text(str)
        self.name = (name or "")[:8] or f"玩家{self.id}"
        self.money = START_MONEY
        self.playing = True
        self.room = None
        self.reset_round()

    def reset_round(self):
        self.ready = False
        self.in_round = False
        self.grab = None          # 斗牛抢庄倍数
        self.contrib = 0          # 本局累计投入筹码
        self.folded = False
        self.matched = False      # 斗牛: 是否跟平
        self.confirmed = False    # 结算后是否点了下一局
        self.money_start = 0
        self.cards = []           # [(rank, suit), ...]
        self.revealed = False
        self.ctype = None
        self.delta = 0
        # 炸金花
        self.seen = False         # 是否已看牌
        self.active = False       # 是否未弃牌(炸金花)

    async def send_obj(self, obj):
        import json
        try:
            await self.conn.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass


# ================================================================ 房间基类
class Room:
    game = ""

    def __init__(self, code):
        self.code = code
        self.players = []
        self.phase = "waiting"
        self.countdown = 0
        self.timer = None
        self.round_no = 0
        self.last_results = []
        self.stake = BASE_SCORE
        self.turn = None
        self.total_rounds = 0      # 本场设定把数, 0=不限
        self.history = []          # 每把输赢记录 [{round, rows:[...]}]

    # ---------------- 基础工具
    def active(self):
        return [p for p in self.players if p.in_round]

    def seated(self):
        return [p for p in self.players if p.playing]

    def cancel_timer(self):
        if self.timer is not None and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer = None

    def set_timer(self, seconds, done):
        self.cancel_timer()

        async def run():
            self.countdown = seconds
            self.broadcast()
            while self.countdown > 0:
                await asyncio.sleep(1)
                self.countdown -= 1
                self.broadcast()
            await done()

        self.timer = asyncio.create_task(run())

    def send(self, p, obj):
        asyncio.create_task(p.send_obj(obj))

    def broadcast(self):
        for p in list(self.players):
            self.send(p, {"type": "state", **self.state_for(p)})

    def sys_msg(self, text):
        for q in list(self.players):
            self.send(q, {"type": "chat", "name": "系统", "text": text})

    def base_state(self, p):
        return {
            "game": self.game,
            "room": self.code,
            "phase": self.phase,
            "countdown": self.countdown,
            "roundNo": self.round_no,
            "base": BASE_SCORE,
            "stake": self.stake,
            "turnId": self.turn.id if self.turn else None,
            "players": [],
            "you": p.id,
            "results": self.last_results,
            "totalRounds": self.total_rounds,
            "history": self.history,
        }

    def new_deck(self):
        deck = [(r, s) for r in range(1, 14) for s in range(4)]
        random.shuffle(deck)
        return deck

    # ---------------- 通用动作
    def on_ready(self, p):
        if self.phase != "waiting" or p in self.active():
            return
        p.ready = not p.ready
        self.broadcast()
        ready_players = [q for q in self.seated() if q.ready]
        if len(ready_players) >= 2 and all(q.ready for q in self.seated()):
            asyncio.create_task(self.begin(ready_players))

    def begin(self, players):
        raise NotImplementedError

    def on_next(self, p):
        if self.phase != "settle":
            return
        p.confirmed = True
        self.broadcast()
        seated = self.seated()
        if seated and all(q.confirmed for q in seated):
            self.cancel_timer()
            asyncio.create_task(self.finish_or_next())

    async def finish_or_next(self):
        """达到设定把数 → 进入 finished(总记录); 否则开下一局"""
        if self.total_rounds and self.round_no >= self.total_rounds:
            self.cancel_timer()
            self.phase = "finished"
            self.turn = None
            self.countdown = 0
            self.broadcast()
        else:
            await self.next_round()

    def on_restart(self, p):
        """全场结束后: 再来一场(保留金币, 清空把数记录)"""
        if self.phase != "finished":
            return
        self.cancel_timer()
        self.round_no = 0
        self.history = []
        self.last_results = []
        self.stake = BASE_SCORE
        self.turn = None
        self.phase = "waiting"
        for q in self.players:
            q.reset_round()
        self.sys_msg("新一场开始, 请大家准备!")
        self.broadcast()

    def record_round(self):
        self.history.append({"round": self.round_no, "rows": self.last_results})
        if len(self.history) > 200:
            self.history.pop(0)

    async def next_round(self):
        self.last_results = []
        candidates = [p for p in self.seated() if p.money >= BASE_SCORE]
        if len(candidates) < 2:
            self.abort_round()
        else:
            await self.begin(candidates)

    def abort_round(self):
        self.cancel_timer()
        self.phase = "waiting"
        self.countdown = 0
        self.last_results = []
        self.stake = BASE_SCORE
        self.turn = None
        for p in self.players:
            p.reset_round()
        self.broadcast()

    def on_relief(self, p):
        if p.money < BASE_SCORE:
            p.money += START_MONEY
            self.broadcast()

    def on_chat(self, p, text):
        text = str(text or "")[:60].strip()
        if text:
            for q in list(self.players):
                self.send(q, {"type": "chat", "name": p.name, "text": text})

    def on_leave_round(self, p):
        raise NotImplementedError

    def remove_player(self, p):
        raise NotImplementedError


# ================================================================ 欢乐斗牛
class DouniuRoom(Room):
    game = "douniu"

    def __init__(self, code):
        super().__init__(code)
        self.banker = None
        self.bet_order = []

    def state_for(self, p):
        st = self.base_state(p)
        st["bankerId"] = self.banker.id if self.banker else None
        for q in self.players:
            info = {
                "id": q.id, "name": q.name, "money": q.money,
                "ready": q.ready, "playing": q.playing,
                "inRound": q.in_round, "grab": q.grab,
                "contrib": q.contrib, "folded": q.folded,
                "confirmed": q.confirmed, "revealed": q.revealed,
                "isBanker": self.banker is q, "delta": q.delta,
                "cards": [], "ctype": None,
            }
            if q is p and q.cards:
                info["cards"] = q.cards
                if q.revealed and q.ctype:
                    info["ctype"] = q.ctype
            elif q.revealed and q.cards:
                info["cards"] = q.cards
                info["ctype"] = q.ctype
            st["players"].append(info)
        return st

    # ---------------- 阶段流转
    async def begin(self, players):
        """开始新的一局(抢庄阶段)"""
        self.cancel_timer()
        self.round_no += 1
        for q in self.players:
            q.reset_round()
        for q in players:
            q.in_round = True
        self.banker = None
        self.last_results = []
        self.phase = "grab"
        self.broadcast()
        self.set_timer(GRAB_TIME, self.end_grab)

    async def end_grab(self):
        act = self.active()
        if len(act) < 2:
            return self.abort_round()
        for p in act:
            if p.grab is None:
                p.grab = 0
        mx = max(p.grab for p in act)
        if mx <= 0:
            self.banker = random.choice(act)
            self.banker.grab = 1
        else:
            self.banker = random.choice([p for p in act if p.grab == mx])
        deck = self.new_deck()
        for i, p in enumerate(act):
            p.cards = deck[i * 5:(i + 1) * 5]
            p.money_start = p.money
        self.stake = BASE_SCORE
        idx = act.index(self.banker)
        self.bet_order = act[idx + 1:] + act[:idx]
        for p in self.bet_order:
            p.matched = False
        self.turn = self.bet_order[0] if self.bet_order else None
        self.phase = "bet"
        self.broadcast()
        if self.turn:
            self.set_timer(TURN_TIME, self.turn_timeout)
        else:
            asyncio.create_task(self.showdown())

    # ---------------- 炸金花式下注 ----------------
    async def turn_timeout(self):
        p = self.turn
        if self.phase != "bet" or p is None or p.folded or p not in self.players:
            return
        if p.money >= self.stake:
            self.do_call(p)
        else:
            self.do_fold(p)

    def next_turn(self):
        alive = [p for p in self.bet_order if not p.folded and p in self.players]
        if not alive or all(p.matched for p in alive):
            self.turn = None
            asyncio.create_task(self.showdown())
            return
        order = self.bet_order
        i = order.index(self.turn) if self.turn in order else -1
        n = len(order)
        for step in range(1, n + 1):
            q = order[(i + step) % n]
            if not q.folded and not q.matched and q in self.players:
                self.turn = q
                break
        else:
            self.turn = None
            asyncio.create_task(self.showdown())
            return
        self.set_timer(TURN_TIME, self.turn_timeout)
        self.broadcast()

    def do_call(self, p):
        amt = min(self.stake, p.money)
        p.money -= amt
        p.contrib += amt
        p.matched = True
        self.broadcast()
        self.next_turn()

    def do_raise(self, p):
        if self.stake >= BASE_SCORE * MAX_STAKE_MULT:
            return self.do_call(p)
        self.stake *= 2
        amt = min(self.stake, p.money)
        p.money -= amt
        p.contrib += amt
        for q in self.bet_order:
            if q is not p and not q.folded:
                q.matched = False
        p.matched = True
        self.broadcast()
        self.next_turn()

    def do_fold(self, p):
        p.folded = True
        p.matched = True
        if self.banker:
            self.banker.money += p.contrib
        p.contrib = 0
        self.broadcast()
        self.next_turn()

    def on_action(self, p, action, target=None):
        if self.phase != "bet" or not p.in_round or p.folded:
            return
        if self.turn is not p:
            return
        if action == "call":
            self.do_call(p)
        elif action == "raise":
            self.do_raise(p)
        elif action == "fold":
            self.do_fold(p)

    def on_grab(self, p, v):
        if self.phase != "grab" or not p.in_round or p.grab is not None:
            return
        p.grab = max(0, min(4, int(v)))
        self.broadcast()
        if all(q.grab is not None for q in self.active()):
            self.cancel_timer()
            asyncio.create_task(self.end_grab())

    # ---------------- 亮牌与结算 ----------------
    async def showdown(self):
        self.cancel_timer()
        act = self.active()
        if len(act) < 2:
            return self.abort_round()
        for p in act:
            if not p.folded:
                p.revealed = True
                p.ctype = calc_cards([c[0] for c in p.cards])
        self.phase = "reveal"
        self.broadcast()
        self.set_timer(REVEAL_TIME, self.settle)

    async def settle(self):
        act = self.active()
        b = self.banker
        bm = b.ctype["mult"]
        for p in act:
            if p is b or p.folded:
                continue
            if p.ctype["mult"] > bm:
                win_amount = min(p.contrib * p.ctype["mult"], max(b.money, 0))
                b.money -= win_amount
                p.money += p.contrib + win_amount
            else:
                b.money += p.contrib
                extra = max(0, min(p.contrib * (bm - 1), p.money))
                p.money -= extra
                b.money += extra
        for p in act:
            p.money = max(0, p.money)
            p.delta = p.money - p.money_start
            p.confirmed = False
        self.last_results = [{
            "id": p.id, "name": p.name,
            "ctype": "弃牌" if (p.folded and p is not b) else p.ctype["name"],
            "delta": p.delta, "win": p.delta > 0, "banker": p is b,
        } for p in act]
        self.record_round()
        self.phase = "settle"
        self.broadcast()
        self.set_timer(SETTLE_TIME, self.finish_or_next)

    # ---------------- 玩家离开
    def on_leave_round(self, p):
        if self.phase == "waiting":
            p.ready = False
            self.broadcast()
            return
        if not p.in_round:
            return
        was_turn = False
        if self.phase == "bet" and p is not self.banker and not p.folded:
            was_turn = self.turn is p
            p.folded = True
            p.matched = True
            if self.banker:
                self.banker.money += p.contrib
            p.contrib = 0
        p.in_round = False
        p.revealed = True
        if p is self.banker or len(self.active()) < 2:
            self.abort_round()
        else:
            if was_turn:
                self.next_turn()
            else:
                self.broadcast()

    def remove_player(self, p):
        if p in self.players:
            self.players.remove(p)
        if not self.players:
            return
        if p.in_round:
            if self.phase == "bet" and not p.folded and p in self.bet_order:
                p.folded = True
                p.matched = True
                if self.banker:
                    self.banker.money += p.contrib
                p.contrib = 0
                if self.turn is p:
                    self.next_turn()
            if p is self.banker or len(self.active()) < 2:
                self.abort_round()
            else:
                self.broadcast()
        else:
            self.broadcast()


# ================================================================ 炸金花
class ZhajinhuaRoom(Room):
    game = "zjh"

    def __init__(self, code):
        super().__init__(code)
        self.pot = 0
        self.order = []          # 本局行动顺序

    def active_zjh(self):
        return [p for p in self.players if p.in_round and not p.folded]

    def state_for(self, p):
        st = self.base_state(p)
        st["pot"] = self.pot
        for q in self.players:
            info = {
                "id": q.id, "name": q.name, "money": q.money,
                "ready": q.ready, "playing": q.playing,
                "inRound": q.in_round, "grab": None,
                "contrib": q.contrib, "folded": q.folded,
                "confirmed": q.confirmed, "revealed": q.revealed,
                "isBanker": False, "delta": q.delta,
                "seen": q.seen, "active": q.active,
                "cards": [], "ctype": None,
            }
            # 自己的牌始终下发; 别人的牌只在亮牌阶段可见
            if q is p and q.cards:
                info["cards"] = q.cards
                if q.seen or q.revealed:
                    info["ctype"] = q.ctype
            elif q.revealed and q.cards and self.phase in ("reveal", "settle"):
                info["cards"] = q.cards
                info["ctype"] = q.ctype
            st["players"].append(info)
        return st

    # ---------------- 开局 ----------------
    async def begin(self, players):
        self.cancel_timer()
        self.round_no += 1
        for q in self.players:
            q.reset_round()
        for q in players:
            if q.money < BASE_SCORE:
                continue
            q.in_round = True
            q.active = True
        act = self.active()
        if len(act) < 2:
            return self.abort_round()
        deck = self.new_deck()
        self.pot = 0
        for i, p in enumerate(act):
            p.money_start = p.money
            ante = min(BASE_SCORE, p.money)
            p.money -= ante
            p.contrib = ante
            self.pot += ante
            p.cards = deck[i * 3:(i + 1) * 3]
            p.ctype = calc_zjh(p.cards)
        self.stake = BASE_SCORE
        # 行动顺序每局轮转
        k = (self.round_no - 1) % len(act)
        self.order = act[k:] + act[:k]
        self.turn = self.order[0]
        self.phase = "bet"
        self.sys_msg(f"第 {self.round_no} 局开始! 每人底注 {BASE_SCORE}, 请行动")
        self.broadcast()
        self.set_timer(TURN_TIME, self.turn_timeout)

    # ---------------- 行动 ----------------
    def call_cost(self, p):
        return self.stake * (2 if p.seen else 1)

    async def turn_timeout(self):
        p = self.turn
        if self.phase != "bet" or p is None or p.folded or p not in self.players:
            return
        if p.money >= self.call_cost(p):
            self.after_call(p, silent=False, auto=True)
        else:
            self.after_fold(p, auto=True)

    def after_call(self, p, silent=False, auto=False):
        cost = min(self.call_cost(p), p.money)
        p.money -= cost
        p.contrib += cost
        self.pot += cost
        p.matched = True
        if not silent:
            self.sys_msg(f"{p.name} {'自动跟注' if auto else '跟注'} {cost} (奖池 {self.pot})")
        self.advance()

    def after_fold(self, p, auto=False):
        p.folded = True
        p.active = False
        p.matched = True
        self.sys_msg(f"{p.name} {'超时自动弃牌' if auto else '弃牌'}")
        if len(self.active_zjh()) <= 1:
            self.end_hand()
        else:
            self.advance()

    def advance(self):
        """轮到下一位未跟平的玩家; 全部跟平则从当前行动者的下一位开启新一轮跟注"""
        if self.phase != "bet":
            return
        act = self.active_zjh()
        nxt = None
        if self.turn and self.turn in self.order:
            i = self.order.index(self.turn)
            n = len(self.order)
            for step in range(1, n + 1):
                q = self.order[(i + step) % n]
                if q in act and not q.matched:
                    nxt = q
                    break
        if nxt is None:
            # 全部跟平: 重置跟平标记, 从当前行动者的下一位继续(炸金花可无限圈)
            for q in act:
                q.matched = False
            start = self.order.index(self.turn) if self.turn in self.order else -1
            n = len(self.order)
            for step in range(1, n + 1):
                q = self.order[(start + step) % n]
                if q in act:
                    nxt = q
                    break
        if nxt is None:
            self.end_hand()
            return
        self.turn = nxt
        self.set_timer(TURN_TIME, self.turn_timeout)
        self.broadcast()

    def on_action(self, p, action, target=None):
        if self.phase != "bet" or not p.in_round or p.folded or self.turn is not p:
            return
        if action == "look":
            if p.seen:
                return
            p.seen = True
            self.sys_msg(f"{p.name} 看牌了")
            self.broadcast()
            return
        if action == "call":
            self.after_call(p)
        elif action == "raise":
            if self.stake >= BASE_SCORE * MAX_STAKE_MULT:
                return self.after_call(p)
            self.stake *= 2
            for q in self.active_zjh():
                q.matched = False
            cost = min(self.call_cost(p), p.money)
            p.money -= cost
            p.contrib += cost
            self.pot += cost
            p.matched = True
            self.sys_msg(f"{p.name} 加注! 底注变为 {self.stake} (奖池 {self.pot})")
            self.advance()
        elif action == "fold":
            self.after_fold(p)
        elif action == "compare":
            self.do_compare(p, target)

    def do_compare(self, p, target):
        t = next((q for q in self.players if q.id == target), None)
        if t is None or t is p or not t.in_round or t.folded:
            return
        cost = self.call_cost(p)
        if p.money < cost:
            return
        p.money -= cost
        p.contrib += cost
        self.pot += cost
        p.revealed = True
        t.revealed = True
        pa = {"value": p.ctype["value"], "cards": p.cards}
        ta = {"value": t.ctype["value"], "cards": t.cards}
        if zjh_beat(pa, ta):
            loser = t
        else:
            loser = p  # 平局算比牌者输
        loser.folded = True
        loser.active = False
        loser.matched = True
        p.matched = True
        self.sys_msg(f"{p.name} 与 {t.name} 比牌: {loser.name} 输了")
        if len(self.active_zjh()) <= 1:
            self.end_hand()
        else:
            self.advance()

    # ---------------- 结束与结算 ----------------
    def end_hand(self):
        self.cancel_timer()
        act = self.active_zjh()
        if len(act) != 1:
            return self.abort_round()
        w = act[0]
        for q in self.players:
            if q.in_round and not q.folded:
                q.revealed = True
        self.turn = None
        self.phase = "reveal"
        self.sys_msg(f"{w.name} 赢得奖池 {self.pot}!")
        self.broadcast()
        self.set_timer(REVEAL_TIME, self.settle)

    async def settle(self):
        act = [p for p in self.players if p.in_round]
        winners = [p for p in act if not p.folded]
        if winners:
            winners[0].money += self.pot
        for p in act:
            p.money = max(0, p.money)
            p.delta = p.money - p.money_start
            p.confirmed = False
        self.last_results = [{
            "id": p.id, "name": p.name,
            "ctype": "弃牌" if p.folded else p.ctype["name"],
            "delta": p.delta, "win": not p.folded, "banker": False,
        } for p in act]
        self.record_round()
        self.phase = "settle"
        self.broadcast()
        self.set_timer(SETTLE_TIME, self.finish_or_next)

    async def next_round(self):
        self.last_results = []
        self.pot = 0
        candidates = [p for p in self.seated() if p.money >= BASE_SCORE]
        if len(candidates) < 2:
            self.abort_round()
        else:
            await self.begin(candidates)

    def abort_round(self):
        self.pot = 0
        self.order = []
        super().abort_round()

    # ---------------- 玩家离开
    def on_leave_round(self, p):
        if self.phase == "waiting":
            p.ready = False
            self.broadcast()
            return
        if not p.in_round:
            return
        was_turn = self.turn is p
        if not p.folded:
            p.folded = True
            p.active = False
            p.matched = True
            self.sys_msg(f"{p.name} 中途退出, 视为弃牌")
        p.in_round = False
        if len(self.active_zjh()) <= 1:
            self.end_hand()
        elif was_turn:
            self.advance()
        else:
            self.broadcast()

    def remove_player(self, p):
        if p in self.players:
            self.players.remove(p)
        if not self.players:
            return
        if p.in_round and not p.folded and self.phase in ("bet", "reveal"):
            was_turn = self.turn is p
            p.folded = True
            p.active = False
            p.matched = True
            if p not in self.order:
                self.order.append(p)
            if len(self.active_zjh()) <= 1:
                self.end_hand()
            elif was_turn:
                self.advance()
            else:
                self.broadcast()
        else:
            self.broadcast()
