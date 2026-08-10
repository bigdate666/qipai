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

GAMES = ("douniu", "zjh", "ddz")
GAME_NAMES = {"douniu": "欢乐斗牛", "zjh": "炸金花", "ddz": "斗地主"}

DDZ_CALL_TIME = 15     # 叫分倒计时
DDZ_DOUBLE_TIME = 10   # 加倍倒计时
DDZ_TURN_TIME = 20     # 出牌倒计时


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

    # ---------------- 行动 ----------------
    def call_cost(self, p):
        return self.stake * (2 if p.seen else 1)

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


# ================================================================ 斗地主
class DdzRoom(Room):
    game = "ddz"

    def __init__(self, code):
        super().__init__(code)
        self.landlord = None
        self.top_cards = []
        self.call_order = []
        self.call_vals = {}
        self.call_max = 0
        self.doubled = {}
        self.order = []
        self.last_play = None      # {pid, cards, type}
        self.pass_cnt = 0
        self.play_cnt = {}
        self.bomb_cnt = 0
        self.base_mult = 1
        self.played = {}           # pid -> 最近一次出的牌
        self.passed = {}           # pid -> 是否刚说不要
        self.spring = False
        self.settle_mult = 1

    # ---------------- 状态 ----------------
    def state_for(self, p):
        st = self.base_state(p)
        st["landlordId"] = self.landlord.id if self.landlord else None
        st["topCards"] = self.top_cards if self.phase in ("double", "play", "settle") else []
        st["lastPlay"] = self.last_play
        st["played"] = {str(k): v for k, v in self.played.items()}
        st["passed"] = {str(k): v for k, v in self.passed.items()}
        st["baseMult"] = self.base_mult
        st["bombCnt"] = self.bomb_cnt
        st["spring"] = self.spring
        st["settleMult"] = self.settle_mult
        st["callMax"] = self.call_max
        for q in self.players:
            info = {
                "id": q.id, "name": q.name, "money": q.money,
                "ready": q.ready, "playing": q.playing,
                "inRound": q.in_round, "grab": None,
                "contrib": 0, "folded": False,
                "confirmed": q.confirmed, "revealed": q.revealed,
                "isBanker": self.landlord is q, "delta": q.delta,
                "cards": [], "ctype": None,
                "cardCount": len(q.cards),
                "callVal": self.call_vals.get(q.id),
                "doubled": self.doubled.get(q.id),
            }
            if q is p:
                info["cards"] = q.cards
            elif q.revealed and self.phase == "settle":
                info["cards"] = q.cards
            st["players"].append(info)
        return st

    # ---------------- 开局 ----------------
    async def begin(self, players):
        self.cancel_timer()
        candidates = [q for q in players if q.money >= BASE_SCORE]
        if len(candidates) < 3:
            self.sys_msg("斗地主需要 3 名玩家, 请等待好友加入")
            self.abort_round()
            return
        act = candidates[:3]
        self.round_no += 1
        for q in self.players:
            q.reset_round()
        for q in act:
            q.in_round = True
            q.money_start = q.money
        self._deal(act)
        k = (self.round_no - 1) % 3
        self.call_order = act[k:] + act[:k]
        self.turn = self.call_order[0]
        self.phase = "call"
        self.sys_msg(f"第 {self.round_no} 局开始! 请 {self.turn.name} 先叫分")
        self.broadcast()
        self.set_timer(DDZ_CALL_TIME, self.call_timeout)

    def _deal(self, act):
        deck = ddz_deck()
        for i, p in enumerate(act):
            p.cards = ddz_sort(deck[i * 17:(i + 1) * 17])
        self.top_cards = ddz_sort(deck[51:])
        self.landlord = None
        self.last_play = None
        self.pass_cnt = 0
        self.bomb_cnt = 0
        self.base_mult = 1
        self.spring = False
        self.play_cnt = {p.id: 0 for p in act}
        self.doubled = {}
        self.call_vals = {}
        self.call_max = 0
        self.played = {}
        self.passed = {}

    async def redeal(self):
        """无人叫分 → 重新发牌(不计局数)"""
        act = self.active()
        self._deal(act)
        random.shuffle(act)
        self.call_order = act
        self.turn = act[0]
        self.phase = "call"
        self.broadcast()
        self.set_timer(DDZ_CALL_TIME, self.call_timeout)

    # ---------------- 叫分 ----------------
    async def call_timeout(self):
        p = self.turn
        if self.phase != "call" or p is None or p not in self.players:
            return
        self.sys_msg(f"{p.name} 超时未叫, 视为不叫")
        self._after_call(p, 0)

    def on_call(self, p, v):
        if self.phase != "call" or self.turn is not p or p.id in self.call_vals:
            return
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = 0
        v = v if v in (1, 2, 3) and v > self.call_max else 0
        self._after_call(p, v)

    def _after_call(self, p, v):
        self.call_vals[p.id] = v
        self.call_max = max(self.call_max, v)
        self.broadcast()
        if v == 3 or all(q.id in self.call_vals for q in self.call_order):
            self.cancel_timer()
            self.end_call()
            return
        i = self.call_order.index(p)
        self.turn = self.call_order[(i + 1) % len(self.call_order)]
        self.set_timer(DDZ_CALL_TIME, self.call_timeout)

    def end_call(self):
        if self.call_max == 0:
            self.sys_msg("无人叫地主, 重新发牌")
            asyncio.create_task(self.redeal())
            return
        self.landlord = next(q for q in self.call_order
                             if self.call_vals.get(q.id) == self.call_max)
        self.base_mult = self.call_max
        self.landlord.cards = ddz_sort(self.landlord.cards + self.top_cards)
        self.phase = "double"
        self.turn = None
        self.doubled = {}
        self.sys_msg(f"👑 {self.landlord.name} 成为地主! 底分倍数 ×{self.base_mult}, 农民请选择是否加倍")
        self.broadcast()
        self.set_timer(DDZ_DOUBLE_TIME, self.double_timeout)

    # ---------------- 加倍 ----------------
    async def double_timeout(self):
        for q in self.active():
            if q is not self.landlord and q.id not in self.doubled:
                self.doubled[q.id] = False
        self.start_play()

    def on_double(self, p, v):
        if self.phase != "double" or not p.in_round or p is self.landlord:
            return
        if p.id in self.doubled:
            return
        self.doubled[p.id] = bool(v)
        self.sys_msg(f"{p.name} 选择{'加倍' if v else '不加倍'}")
        self.broadcast()
        farmers = [q for q in self.active() if q is not self.landlord]
        if all(q.id in self.doubled for q in farmers):
            self.cancel_timer()
            self.start_play()

    # ---------------- 出牌 ----------------
    def start_play(self):
        self.cancel_timer()
        self.phase = "play"
        act = self.active()
        i = act.index(self.landlord)
        self.order = act[i:] + act[:i]
        self.turn = self.landlord
        self.last_play = None
        self.pass_cnt = 0
        self.broadcast()
        self.set_timer(DDZ_TURN_TIME, self.play_timeout)

    async def play_timeout(self):
        p = self.turn
        if self.phase != "play" or p is None or p not in self.players:
            return
        if self.last_play is None or self.last_play["pid"] == p.id:
            sug = ddz_hint(p.cards, None)
            self.sys_msg(f"{p.name} 超时自动出牌")
            self.do_play(p, sug)
        else:
            self.sys_msg(f"{p.name} 超时不要")
            self.do_pass(p)

    def on_pass(self, p):
        if self.phase != "play" or self.turn is not p:
            return
        if self.last_play is None or self.last_play["pid"] == p.id:
            return
        self.do_pass(p)

    def do_pass(self, p):
        self.passed[p.id] = True
        self.played[p.id] = []
        self.pass_cnt += 1
        if self.pass_cnt >= 2:
            self.last_play = None
            self.pass_cnt = 0
        self._next_turn()

    def on_play(self, p, cards):
        if self.phase != "play" or self.turn is not p:
            return
        if not isinstance(cards, list) or not cards:
            return
        hand = list(p.cards)
        picked = []
        for c in cards:
            try:
                c = [int(c[0]), int(c[1])]
            except (TypeError, ValueError, IndexError):
                return
            t = tuple(c)
            if t in hand:
                hand.remove(t)
                picked.append(t)
            else:
                return
        if len(picked) != len(cards):
            return
        t = ddz_type(picked)
        if t is None:
            return
        if self.last_play and self.last_play["pid"] != p.id:
            if not ddz_beat(t, self.last_play["type"]):
                return
        self.do_play(p, picked, t)

    def do_play(self, p, picked, t=None):
        if not picked:
            return self.do_pass(p)
        t = t or ddz_type(picked)
        if t is None:
            return
        for c in picked:
            p.cards.remove(tuple(c))
        self.played[p.id] = picked
        self.passed[p.id] = False
        self.play_cnt[p.id] = self.play_cnt.get(p.id, 0) + 1
        self.pass_cnt = 0
        self.last_play = {"pid": p.id, "cards": picked, "type": t}
        if t["t"] in ("bomb", "rocket"):
            self.bomb_cnt += 1
            self.sys_msg(f"💥 {p.name} 打出{DDZ_TYPE_NAMES[t['t']]}! 倍数 ×2")
        self.broadcast()
        if not p.cards:
            asyncio.create_task(self.settle())
            return
        self._next_turn()

    def _next_turn(self):
        i = self.order.index(self.turn)
        self.turn = self.order[(i + 1) % len(self.order)]
        self.set_timer(DDZ_TURN_TIME, self.play_timeout)
        self.broadcast()

    def on_hint(self, p):
        if self.phase != "play" or self.turn is not p:
            return
        last = None
        if self.last_play and self.last_play["pid"] != p.id:
            last = self.last_play["type"]
        sug = ddz_hint(p.cards, last)
        self.send(p, {"type": "hint", "cards": sug or []})

    # ---------------- 结算 ----------------
    async def settle(self):
        self.cancel_timer()
        act = self.active()
        landlord_win = len(self.landlord.cards) == 0
        if landlord_win:
            self.spring = all(self.play_cnt.get(q.id, 0) == 0
                              for q in act if q is not self.landlord)
        else:
            self.spring = self.play_cnt.get(self.landlord.id, 0) <= 1
        mult = self.base_mult * (2 ** self.bomb_cnt) * (2 if self.spring else 1)
        self.settle_mult = mult
        for f in act:
            if f is self.landlord:
                continue
            unit = BASE_SCORE * mult * (2 if self.doubled.get(f.id) else 1)
            if landlord_win:
                amt = min(unit, f.money, self.landlord.money)
                f.money -= amt
                self.landlord.money += amt
            else:
                amt = min(unit, self.landlord.money)
                self.landlord.money -= amt
                f.money += amt
        for q in act:
            q.money = max(0, q.money)
            q.delta = q.money - q.money_start
            q.revealed = True
            q.confirmed = False
        spring_txt = ""
        if self.spring:
            spring_txt = "·春天" if landlord_win else "·反春天"
        self.last_results = []
        for q in act:
            role = "地主" if q is self.landlord else "农民"
            extra = ""
            if q is not self.landlord and self.doubled.get(q.id):
                extra = "·加倍"
            self.last_results.append({
                "id": q.id, "name": q.name,
                "ctype": f"{role}{spring_txt}{extra}",
                "delta": q.delta, "win": q.delta > 0,
                "banker": q is self.landlord,
            })
        self.spring_txt = spring_txt
        self.record_round()
        self.phase = "settle"
        winner = self.landlord.name if landlord_win else "农民方"
        self.sys_msg(f"🏁 {winner} 获胜! 倍数 ×{mult} (底×{self.base_mult} 炸×{2 ** self.bomb_cnt}{' 春天×2' if self.spring else ''})")
        self.broadcast()
        self.set_timer(SETTLE_TIME, self.finish_or_next)

    # ---------------- 玩家离开 ----------------
    def on_leave_round(self, p):
        if self.phase == "waiting":
            p.ready = False
            self.broadcast()
            return
        if not p.in_round:
            return
        self.sys_msg(f"{p.name} 退出本局, 本局解散")
        self.abort_round()

    def remove_player(self, p):
        if p in self.players:
            self.players.remove(p)
        if not self.players:
            return
        if p.in_round and self.phase not in ("settle", "finished"):
            self.sys_msg(f"{p.name} 离开房间, 本局解散")
            self.abort_round()
        else:
            self.broadcast()


# ================================================================ 斗地主牌型
DDZ_TYPE_NAMES = {
    "rocket": "王炸", "bomb": "炸弹", "single": "单张", "pair": "对子",
    "triple": "三张", "triple_one": "三带一", "triple_pair": "三带二",
    "straight": "顺子", "pair_straight": "连对", "plane": "飞机",
    "plane_singles": "飞机带单", "plane_pairs": "飞机带对",
    "four_two_single": "四带二", "four_two_pair": "四带两对",
}


def ddz_deck():
    deck = [(r, s) for r in range(3, 16) for s in range(4)]
    deck.append((16, 4))   # 小王
    deck.append((17, 4))   # 大王
    random.shuffle(deck)
    return deck


def ddz_sort(cards):
    return sorted(cards, key=lambda c: (c[0], c[1]), reverse=True)


def ddz_type(cards):
    """识别牌型, 返回 {t, main, len} 或 None; 牌用 (rank, suit), 16=小王 17=大王"""
    n = len(cards)
    if not n:
        return None
    rs = sorted(c[0] for c in cards)
    if n == 2 and rs == [16, 17]:
        return {"t": "rocket", "main": 17, "len": 2}
    if n == 1:
        return {"t": "single", "main": rs[0], "len": 1}
    if any(r >= 16 for r in rs):
        return None
    cnt = {}
    for r in rs:
        cnt[r] = cnt.get(r, 0) + 1
    g2 = sorted(r for r in cnt if cnt[r] == 2)
    g3 = sorted(r for r in cnt if cnt[r] == 3)
    g4 = sorted(r for r in cnt if cnt[r] == 4)

    def run_ok(lst, k, top_min=3):
        """lst 中是否存在 k 张连续(≤A), 返回最高的一段(要求顶 > top_min-1)"""
        lst = [r for r in lst if r <= 14]
        for i in range(len(lst) - k + 1):
            seg = lst[i:i + k]
            if seg[-1] - seg[0] == k - 1 and seg[-1] >= top_min:
                return seg
        return None

    if n == 2 and g2 and len(cnt) == 1:
        return {"t": "pair", "main": rs[0], "len": 2}
    if n == 3 and g3:
        return {"t": "triple", "main": rs[0], "len": 3}
    if n == 4 and g4:
        return {"t": "bomb", "main": rs[0], "len": 4}
    if n == 4 and g3:
        return {"t": "triple_one", "main": g3[0], "len": 4}
    if n == 5 and g3 and g2:
        return {"t": "triple_pair", "main": g3[0], "len": 5}
    if n >= 5 and all(cnt[r] == 1 for r in cnt):
        seg = run_ok(sorted(cnt), n)
        if seg and len(seg) == n:
            return {"t": "straight", "main": seg[-1], "len": n}
    if n >= 6 and n % 2 == 0 and all(cnt[r] == 2 for r in cnt):
        seg = run_ok(sorted(cnt), n // 2)
        if seg and len(seg) == n // 2:
            return {"t": "pair_straight", "main": seg[-1], "len": n}
    if n >= 6 and n % 3 == 0 and all(cnt[r] == 3 for r in cnt):
        seg = run_ok(sorted(cnt), n // 3)
        if seg and len(seg) == n // 3:
            return {"t": "plane", "main": seg[-1], "len": n}
    if n >= 8 and n % 4 == 0:
        k = n // 4
        seg = run_ok(g3 + g4, k)
        if seg:
            return {"t": "plane_singles", "main": seg[-1], "len": n}
    if n >= 10 and n % 5 == 0:
        k = n // 5
        seg = run_ok(g3 + g4, k)
        if seg:
            rest = dict(cnt)
            for r in seg:
                rest[r] -= 3
                if rest[r] <= 0:
                    del rest[r]
            if all(c % 2 == 0 for c in rest.values()):
                return {"t": "plane_pairs", "main": seg[-1], "len": n}
    if n == 6 and g4:
        return {"t": "four_two_single", "main": g4[0], "len": 6}
    if n == 8 and g4:
        rest = [r for r in sorted(cnt) if r != g4[0]]
        if all(cnt[r] == 2 for r in rest) and len(rest) == 2:
            return {"t": "four_two_pair", "main": g4[0], "len": 8}
    return None


def ddz_beat(a, b):
    """a 能否压过 b"""
    if a["t"] == "rocket":
        return True
    if b["t"] == "rocket":
        return False
    if a["t"] == "bomb" and b["t"] != "bomb":
        return True
    if a["t"] != b["t"] or a["len"] != b["len"]:
        return False
    return a["main"] > b["main"]


def ddz_hint(hand, last):
    """提示出牌: 返回建议出的牌列表; None 表示要不起"""
    cnt = {}
    for r, s in hand:
        cnt[r] = cnt.get(r, 0) + 1
    ranks = sorted(cnt)

    def take(r, k):
        out = []
        for c in hand:
            if c[0] == r and k > 0:
                out.append(c)
                k -= 1
        return out

    if last is None:
        t = ddz_type(hand)
        if t:
            return list(hand)
        return take(ranks[0], 1)

    t, m, L = last["t"], last["main"], last["len"]
    if t == "single":
        for r in ranks:
            if r > m:
                return take(r, 1)
    elif t == "pair":
        for r in ranks:
            if r > m and cnt[r] >= 2:
                return take(r, 2)
    elif t == "triple":
        for r in ranks:
            if r > m and cnt[r] >= 3:
                return take(r, 3)
    elif t == "triple_one":
        for r in ranks:
            if r > m and cnt[r] >= 3 and any(x != r for x in ranks):
                w = min(x for x in ranks if x != r)
                return take(r, 3) + take(w, 1)
    elif t == "triple_pair":
        for r in ranks:
            if r > m and cnt[r] >= 3:
                pr = [x for x in ranks if x != r and cnt[x] >= 2]
                if pr:
                    return take(r, 3) + take(pr[0], 2)
    elif t == "straight":
        for start in range(3, 15 - L + 1):
            top = start + L - 1
            if top > m and all(cnt.get(x, 0) >= 1 for x in range(start, top + 1)):
                return [take(x, 1)[0] for x in range(start, top + 1)]
    elif t == "pair_straight":
        k = L // 2
        for start in range(3, 15 - k + 1):
            top = start + k - 1
            if top > m and all(cnt.get(x, 0) >= 2 for x in range(start, top + 1)):
                out = []
                for x in range(start, top + 1):
                    out += take(x, 2)
                return out
    elif t == "plane":
        k = L // 3
        for start in range(3, 15 - k + 1):
            top = start + k - 1
            if top > m and all(cnt.get(x, 0) >= 3 for x in range(start, top + 1)):
                out = []
                for x in range(start, top + 1):
                    out += take(x, 3)
                return out
    elif t == "plane_singles":
        k = L // 4
        for start in range(3, 15 - k + 1):
            top = start + k - 1
            if top > m and all(cnt.get(x, 0) >= 3 for x in range(start, top + 1)):
                out = []
                for x in range(start, top + 1):
                    out += take(x, 3)
                wings = [c for c in hand if c not in out][:k]
                if len(wings) >= k:
                    return out + wings
    elif t == "plane_pairs":
        k = L // 5
        for start in range(3, 15 - k + 1):
            top = start + k - 1
            if top > m and all(cnt.get(x, 0) >= 3 for x in range(start, top + 1)):
                out = []
                for x in range(start, top + 1):
                    out += take(x, 3)
                rest = {}
                for r in ranks:
                    left = cnt[r] - (3 if start <= r <= top else 0)
                    if left > 0:
                        rest[r] = left
                prs = [r for r in rest if rest[r] >= 2]
                if len(prs) >= k:
                    for r in prs[:k]:
                        out += take(r, 2)
                    return out
    elif t == "four_two_single":
        for r in ranks:
            if r > m and cnt[r] == 4:
                wings = [x for x in ranks if x != r][:2]
                if len(wings) >= 2:
                    return take(r, 4) + take(wings[0], 1) + take(wings[1], 1)
    elif t == "four_two_pair":
        for r in ranks:
            if r > m and cnt[r] == 4:
                prs = [x for x in ranks if x != r and cnt[x] >= 2][:2]
                if len(prs) >= 2:
                    return take(r, 4) + take(prs[0], 2) + take(prs[1], 2)
    # 炸弹 / 王炸
    if t != "bomb":
        for r in ranks:
            if cnt[r] == 4:
                return take(r, 4)
    else:
        for r in ranks:
            if cnt[r] == 4 and r > m:
                return take(r, 4)
    if cnt.get(16) and cnt.get(17):
        return take(16, 1) + take(17, 1)
    return None
