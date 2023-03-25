"""
EvAdventure turn-based combat

This implements a turn-based combat style, where both sides have a little longer time to
choose their next action. If they don't react before a timer runs out, the previous action
will be repeated. This means that a 'twitch' style combat can be created using the same
mechanism, by just speeding up each 'turn'.

The combat is handled with a `Script` shared between all combatants; this tracks the state
of combat and handles all timing elements.

Unlike in base _Knave_, the MUD version's combat is simultaneous; everyone plans and executes
their turns simultaneously with minimum downtime.

This version is simplified to not worry about things like optimal range etc. So a bow can be used
the same as a sword in battle. One could add a 1D range mechanism to add more strategy by requiring
optimizal positioning.

The combat is controlled through a menu:

------------------- main menu
Combat

You have 30 seconds to choose your next action. If you don't decide, you will hesitate and do
nothing. Available actions:

1. [A]ttack/[C]ast spell at <target> using your equipped weapon/spell
3. Make [S]tunt <target/yourself> (gain/give advantage/disadvantage for future attacks)
4. S[W]ap weapon / spell rune
5. [U]se <item>
6. [F]lee/disengage (takes one turn, during which attacks have advantage against you)
8. [H]esitate/Do nothing

You can also use say/emote between rounds.
As soon as all combatants have made their choice (or time out), the round will be resolved
simultaneusly.

-------------------- attack/cast spell submenu

Choose the target of your attack/spell:
0: Yourself              3: <enemy 3> (wounded)
1: <enemy 1> (hurt)
2: <enemy 2> (unharmed)

------------------- make stunt submenu

Stunts are special actions that don't cause damage but grant advantage for you or
an ally for future attacks - or grant disadvantage to your enemy's future attacks.
The effects of stunts start to apply *next* round. The effect does not stack, can only
be used once and must be taken advantage of within 5 rounds.

Choose stunt:
1: Trip <target> (give disadvantage DEX)
2: Feint <target> (get advantage DEX against target)
3: ...

-------------------- make stunt target submenu

Choose the target of your stunt:
0: Yourself                  3: <combatant 3> (wounded)
1: <combatant 1> (hurt)
2: <combatant 2> (unharmed)

-------------------  swap weapon or spell run

Choose the item to wield.
1: <item1>
2: <item2> (two hands)
3: <item3>
4: ...

------------------- use item

Choose item to use.
1: Healing potion (+1d6 HP)
2: Magic pebble (gain advantage, 1 use)
3: Potion of glue (give disadvantage to target)

------------------- Hesitate/Do nothing

You hang back, passively defending.

------------------- Disengage

You retreat, getting ready to get out of combat. Use two times in a row to
leave combat. You flee last in a round.



"""

import random
from collections import defaultdict, deque

from evennia import CmdSet, Command, create_script, default_cmds
from evennia.commands.command import InterruptCommand
from evennia.scripts.scripts import DefaultScript
from evennia.typeclasses.attributes import AttributeProperty
from evennia.utils import dbserialize, delay, evmenu, evtable, logger
from evennia.utils.utils import display_len, inherits_from, list_to_string, pad

from . import rules
from .characters import EvAdventureCharacter
from .enums import ABILITY_REVERSE_MAP, Ability, ObjType
from .npcs import EvAdventureNPC
from .objects import EvAdventureObject

COMBAT_HANDLER_KEY = "evadventure_turnbased_combathandler"
COMBAT_HANDLER_INTERVAL = 30


class CombatFailure(RuntimeError):
    """
    Some failure during actions.

    """


# Combat action classes


class CombatAction:
    """
    Parent class for all actions.

    This represents the executable code to run to perform an action. It is initialized from an
    'action-dict', a set of properties stored in the action queue by each combatant.

    """

    def __init__(self, combathandler, combatant, action_dict):
        """
        Each key-value pair in the action-dict is stored as a property on this class
        for later access.

        Args:
            combatant (EvAdventureCharacter, EvAdventureNPC): The combatant performing
                the action.
            action_dict (dict): A dict containing all properties to initialize on this
                class. This should not be any keys with `_` prefix, since these are
                used internally by the class.

        """
        self.combathandler = combathandler
        self.combatant = combatant

        for key, val in action_dict.items():
            setattr(self, key, val)

    # advantage / disadvantage
    # These should be read as 'does <recipient> have dis/advantaget against <target>'.
    def give_advantage(self, recipient, target, **kwargs):
        self.combathandler.advantage_matrix[recipient][target] = True

    def give_disadvantage(self, recipient, target, **kwargs):
        self.combathandler.disadvantage_matrix[recipient][target] = True

    def has_advantage(self, recipient, target):
        return bool(self.combathandler.advantage_matrix[recipient].pop(target, False)) or (
            target in self.combathandler.fleeing_combatants
        )

    def has_disadvantage(self, recipient, target):
        return bool(self.combathandler.disadvantage_matrix[recipient].pop(target, False)) or (
            recipient in self.combathandler.fleeing_combatants
        )

    def lose_advantage(self, recipient, target):
        self.combathandler.advantage_matrix[recipient][target] = False

    def lose_disadvantage(self, recipient, target):
        self.combathandler.disadvantage_matrix[recipient][target] = False

    def msg(self, message, broadcast=True):
        """
        Convenience route to the combathandler msg-sender mechanism.

        Args:
            message (str): Message to send; use `$You()` and `$You(other.key)` to refer to
                the combatant doing the action and other combatants, respectively.

        """
        self.combathandler.msg(message, combatant=self.combatant, broadcast=broadcast)

    def can_use(self):
        """
        Called to determine if the action is usable with the current settings. This does not
        actually perform the action.

        Returns:
            bool: If this action can be used at this time.

        """
        return True

    def execute(self):
        """
        Perform the action as the combatant. Should normally make use of the properties
        stored on the class during initialization.

        """
        pass

    def post_execute(self):
        """
        Called after execution.
        """
        # most actions abort ongoing fleeing actions.
        self.combathandler.fleeing_combatants.pop(self.combatant, None)


class CombatActionHold(CombatAction):
    """
    Action that does nothing.

    Note:
        Refer to as 'hold'

    action_dict = {
            "key": "hold"
        }
    """


class CombatActionAttack(CombatAction):
    """
    A regular attack, using a wielded weapon.

    action-dict = {
            "key": "attack",
            "target": Character/Object
        }

    Note:
        Refer to as 'attack'

    """

    def execute(self):
        attacker = self.combatant
        weapon = attacker.weapon
        target = self.target

        if weapon.at_pre_use(attacker, target):
            weapon.use(attacker, target, advantage=self.has_advantage(attacker, target))
            weapon.at_post_use(attacker, target)


class CombatActionStunt(CombatAction):
    """
    Perform a stunt the grants a beneficiary (can be self) advantage on their next action against a
    target. Whenever performing a stunt that would affect another negatively (giving them disadvantage
    against an ally, or granting an advantage against them, we need to make a check first. We don't
    do a check if giving an advantage to an ally or ourselves.

    action_dict = {
           "key": "stunt",
           "recipient": Character/NPC,
           "target": Character/NPC,
           "advantage": bool,  # if False, it's a disadvantage
           "stunt_type": Ability,  # what ability (like STR, DEX etc) to use to perform this stunt.
           "defense_type": Ability, # what ability to use to defend against (negative) effects of this
               stunt.
        }

    Note:
        refer to as 'stunt'.

    """

    def execute(self):
        attacker = self.combatant
        recipient = self.recipient  # the one to receive the effect of the stunt
        target = self.target  # the affected by the stunt (can be the same as recipient/combatant)
        txt = ""

        if recipient == target:
            # grant another entity dis/advantage against themselves
            defender = recipient
        else:
            # recipient not same as target; who will defend depends on disadvantage or advantage
            # to give.
            defender = target if self.advantage else recipient

        # trying to give advantage to recipient against target. Target defends against caller
        is_success, _, txt = rules.dice.opposed_saving_throw(
            attacker,
            defender,
            attack_type=self.stunt_type,
            defense_type=self.defense_type,
            advantage=self.has_advantage(attacker, defender),
            disadvantage=self.has_disadvantage(attacker, defender),
        )

        self.msg(f"$You() $conj(attempt) stunt on $You({defender.key}). {txt}")

        # deal with results
        if is_success:
            if self.advantage:
                self.give_advantage(recipient, target)
            else:
                self.give_disadvantage(recipient, target)
            if recipient == self.combatant:
                self.msg(
                    f"$You() $conj(gain) {'advantage' if self.advantage else 'disadvantage'} "
                    f"against $You({target.key})!"
                )
            else:
                self.msg(
                    f"$You() $conj(cause) $You({recipient.key}) "
                    f"to gain {'advantage' if self.advantage else 'disadvantage'} "
                    f"against $You({target.key})!"
                )
            self.msg(
                "|yHaving succeeded, you hold back to plan your next move.|n [hold]",
                broadcast=False,
            )
            self.combathandler.queue_action(attacker, {"key": "hold"})
        else:
            self.msg(f"$You({defender.key}) $conj(resist)! $You() $conj(fail) the stunt.")


class CombatActionUseItem(CombatAction):
    """
    Use an item in combat. This is meant for one-off or limited-use items (so things like
    scrolls and potions, not swords and shields). If this is some sort of weapon or spell rune,
    we refer to the item to determine what to use for attack/defense rolls.

    action_dict = {
            "key": "use",
            "item": Object
            "target": Character/NPC/Object/None
        }

    Note:
        Refer to as 'use'

    """

    def execute(self):

        item = self.item
        user = self.combatant
        target = self.target

        if item.at_pre_use(user, target):
            item.use(
                user,
                target,
                advantage=self.has_advantage(user, target),
                disadvantage=self.has_disadvantage(user, target),
            )
            item.at_post_use(user, target)
        # to back to idle after this
        self.combathandler.queue_action(self.combatant, {"key": "hold"})


class CombatActionWield(CombatAction):
    """
    Wield a new weapon (or spell) from your inventory. This will swap out the one you are currently
    wielding, if any.

    action_dict = {
            "key": "wield",
            "item": Object
        }

    Note:
        Refer to as 'wield'.

    """

    def execute(self):
        self.combatant.equipment.move(self.item)
        self.combathandler.queue_action(self.combatant, {"key": "hold"})


class CombatActionFlee(CombatAction):
    """
    Start (or continue) fleeing/disengaging from combat.

    action_dict = {
           "key": "flee",
        }

    Note:
        Refer to as 'flee'.

    """

    def execute(self):

        combathandler = self.combathandler

        if self.combatant not in combathandler.fleeing_combatants:
            # we record the turn on which we started fleeing
            combathandler.fleeing_combatants[self.combatant] = self.combathandler.turn

        # show how many turns until successful flight
        current_turn = combathandler.turn
        started_fleeing = combathandler.fleeing_combatants[self.combatant]
        flee_timeout = combathandler.flee_timeout
        time_left = flee_timeout - (current_turn - started_fleeing)

        if time_left > 0:
            self.msg(
                "$You() $conj(retreat), being exposed to attack while doing so (will escape in "
                f"{time_left} $pluralize(turn, {time_left}))."
            )

    def post_execute(self):
        """
        We override the default since we don't want to cancel fleeing here.
        """
        pass


class EvAdventureCombatHandler(DefaultScript):
    """
    This script is created when a combat starts. It 'ticks' the combat and tracks
    all sides of it.

    """

    # available actions in combat
    action_classes = {
        "hold": CombatActionHold,
        "attack": CombatActionAttack,
        "stunt": CombatActionStunt,
        "use": CombatActionUseItem,
        "wield": CombatActionWield,
        "flee": CombatActionFlee,
    }

    # how many actions can be queued at a time (per combatant)
    max_action_queue_size = 1

    # fallback action if not selecting anything
    fallback_action_dict = {"key": "hold"}

    # how many turns you must be fleeing before escaping
    flee_timeout = 5

    # persistent storage

    turn = AttributeProperty(0)

    # who is involved in combat, and their action queue,
    # as {combatant: [actiondict, actiondict,...]}
    combatants = AttributeProperty(dict)

    advantage_matrix = AttributeProperty(defaultdict(dict))
    disadvantage_matrix = AttributeProperty(defaultdict(dict))

    fleeing_combatants = AttributeProperty(dict)
    defeated_combatants = AttributeProperty(list)

    # usable script properties
    # .is_active - show if timer is running

    def msg(self, message, combatant=None, broadcast=True):
        """
        Central place for sending messages to combatants. This allows
        for adding any combat-specific text-decoration in one place.

        Args:
            message (str): The message to send.
            combatant (Object): The 'You' in the message, if any.
            broadcast (bool): If `False`, `combatant` must be included and
                will be the only one to see the message. If `True`, send to
                everyone in the location.

        Notes:
            If `combatant` is given, use `$You/you()` markup to create
            a message that looks different depending on who sees it. Use
            `$You(combatant_key)` to refer to other combatants.

        """
        location = self.obj
        location_objs = location.contents

        exclude = []
        if not broadcast and combatant:
            exclude = [obj for obj in location_objs if obj is not combatant]

        location.msg_contents(
            message,
            exclude=exclude,
            from_obj=combatant,
            mapping={locobj.key: locobj for locobj in location_objs},
        )

    def add_combatant(self, combatant):
        """
        Add a new combatant to the battle. Can be called multiple times safely.

        Args:
            *combatants (EvAdventureCharacter, EvAdventureNPC): Any number of combatants to add to
                the combat.
        Returns:
            bool: If this combatant was newly added or not (it was already in combat).

        """
        if combatant not in self.combatants:
            self.combatants[combatant] = deque((), maxlen=self.max_action_queue_size)
            return True
        return False

    def remove_combatant(self, combatant):
        """
        Remove a combatant from the battle. This removes their queue.

        Args:
            combatant (EvAdventureCharacter, EvAdventureNPC): A combatant to add to
                the combat.

        """
        self.combatants.pop(combatant, None)
        # clean up twitch cmdset if it exists
        combatant.cmdset.remove(TwitchCombatCmdSet)
        # clean up menu if it exists

    def start_combat(self, **kwargs):
        """
        This actually starts the combat. It's safe to run this multiple times
        since it will only start combat if it isn't already running.

        """
        if not self.is_active:
            self.start(**kwargs)

    def stop_combat(self):
        """
        Stop the combat immediately.

        """
        for combatant in self.combatants:
            self.remove_combatant(combatant)
        self.stop()
        self.delete()

    def get_sides(self, combatant):
        """
        Get a listing of the two 'sides' of this combat, from the perspective of the provided
        combatant. The sides don't need to be balanced.

        Args:
            combatant (Character or NPC): The one whose sides are to determined.

        Returns:
            tuple: A tuple of lists `(allies, enemies)`, from the perspective of `combatant`.

        Note:
            The sides are found by checking PCs vs NPCs. PCs can normally not attack other PCs, so
            are naturally allies. If the current room has the `allow_pvp` Attribute set, then _all_
            other combatants (PCs and NPCs alike) are considered valid enemies (one could expand
            this with group mechanics).

        """
        if self.obj.allow_pvp:
            # in pvp, everyone else is an ememy
            allies = [combatant]
            enemies = [comb for comb in self.combatants if comb != combatant]
        else:
            # otherwise, enemies/allies depend on who combatant is
            pcs = [comb for comb in self.combatants if inherits_from(comb, EvAdventureCharacter)]
            npcs = [comb for comb in self.combatants if comb not in pcs]
            if combatant in pcs:
                # combatant is a PC, so NPCs are all enemies
                allies = [comb for comb in pcs if comb != combatant]
                enemies = npcs
            else:
                # combatant is an NPC, so PCs are all enemies
                allies = [comb for comb in npcs if comb != combatant]
                enemies = pcs
        return allies, enemies

    def get_combat_summary(self, combatant):
        """
        Get a 'battle report' - an overview of the current state of combat from the perspective
        of one of the sides.

        Args:
            combatant (EvAdventureCharacter, EvAdventureNPC): The combatant to get.

        Returns:
            EvTable: A table representing the current state of combat.

        Example:
        ::

                                        Goblin shaman (Perfect)[attack]
        Gregor (Hurt)[attack]           Goblin brawler(Hurt)[attack]
        Bob (Perfect)[stunt]     vs     Goblin grunt 1 (Hurt)[attack]
                                        Goblin grunt 2 (Perfect)[hold]
                                        Goblin grunt 3 (Wounded)[flee]

        """
        allies, enemies = self.get_sides(combatant)
        # we must include outselves at the top of the list (we are not returned from get_sides)
        allies.insert(0, combatant)
        nallies, nenemies = len(allies), len(enemies)

        # prepare colors and hurt-levels
        allies = [
            f"{ally} ({ally.hurt_level})[{self.get_next_action_dict(ally)['key']}]"
            for ally in allies
        ]
        enemies = [
            f"{enemy} ({enemy.hurt_level})[{self.get_next_action_dict(enemy)['key']}]"
            for enemy in enemies
        ]

        # the center column with the 'vs'
        vs_column = ["" for _ in range(max(nallies, nenemies))]
        vs_column[len(vs_column) // 2] = "|wvs|n"

        # the two allies / enemies columns should be centered vertically
        diff = abs(nallies - nenemies)
        top_empty = diff // 2
        bot_empty = diff - top_empty
        topfill = ["" for _ in range(top_empty)]
        botfill = ["" for _ in range(bot_empty)]

        if nallies >= nenemies:
            enemies = topfill + enemies + botfill
        else:
            allies = topfill + allies + botfill

        # make a table with three columns
        return evtable.EvTable(
            table=[
                evtable.EvColumn(*allies, align="l"),
                evtable.EvColumn(*vs_column, align="c"),
                evtable.EvColumn(*enemies, align="r"),
            ],
            border=None,
            maxwidth=78,
        )

    def queue_action(self, combatant, action_dict):
        """
        Queue an action by adding the new actiondict to the back of the queue. If the
        queue was alrady at max-size, the front of the queue will be discarded.

        Args:
            combatant (EvAdventureCharacter, EvAdventureNPC): A combatant queueing the action.
            action_dict (dict): A dict describing the action class by name along with properties.

        Example:
            If the queue max-size is 3 and was `[a, b, c]` (where each element is an action-dict),
            then using this method to add the new action-dict `d` will lead to a queue `[b, c, d]` -
            that is, adding the new action will discard the one currently at the front of the queue
            to make room.

        """
        self.combatants[combatant].append(action_dict)

        # track who inserted actions this turn (non-persistent)
        did_action = set(self.ndb.did_action or ())
        did_action.add(combatant)
        if len(did_action) >= len(self.combatants):
            # everyone has inserted an action. Start next turn without waiting!
            self.force_repeat()

    def get_next_action_dict(self, combatant, rotate_queue=True):
        """
        Give the action_dict for the next action that will be executed.

        Args:
            combatant (EvAdventureCharacter, EvAdventureNPC): The combatant to get the action for.
            rotate_queue (bool, optional): Rotate the queue after getting the action dict.

        Returns:
            dict: The next action-dict in the queue.

        """
        action_queue = self.combatants[combatant]
        action_dict = action_queue[0] if action_queue else self.fallback_action_dict
        if rotate_queue:
            # rotate the queue to the left so that the first element is now the last one
            action_queue.rotate(-1)
        return action_dict

    def execute_next_action(self, combatant):
        """
        Perform a combatant's next queued action. Note that there is _always_ an action queued,
        even if this action is 'hold'. We don't pop anything from the queue, instead we keep
        rotating the queue. When the queue has a length of one, this means just repeating the
        same action over and over.

        Args:
            combatant (EvAdventureCharacter, EvAdventureNPC): The combatant performing and action.

        Example:
            If the combatant's action queue is `[a, b, c]` (where each element is an action-dict),
            then calling this method will lead to action `a` being performed. After this method, the
            queue will be rotated to the left and be `[b, c, a]` (so next time, `b` will be used).

        """
        # this gets the next dict and rotates the queue
        action_dict = self.get_next_action_dict(combatant)

        # use the action-dict to select and create an action from an action class
        action_class = self.action_classes[action_dict["key"]]
        action = action_class(self, combatant, action_dict)

        action.execute()
        action.post_execute()

    def execute_full_turn(self):
        """
        Perform a full turn of combat, performing everyone's actions in random order.

        """
        self.turn += 1
        # random turn order
        combatants = list(self.combatants.keys())
        random.shuffle(combatants)  # shuffles in place

        # do everyone's next queued combat action
        for combatant in combatants:
            self.execute_next_action(combatant)

        # check if anyone is defeated
        for combatant in list(self.combatants.keys()):
            if combatant.hp <= 0:
                # PCs roll on the death table here, NPCs die. Even if PCs survive, they
                # are still out of the fight.
                combatant.at_defeat()
                self.combatants.pop(combatant)
                self.defeated_combatants.append(combatant)
                self.msg("|r$You() $conj(fall) to the ground, defeated.|n", combatant=combatant)

        # check if anyone managed to flee
        flee_timeout = self.flee_timeout
        for combatant, started_fleeing in self.fleeing_combatants.items():
            if self.turn - started_fleeing >= flee_timeout:
                # if they are still alive/fleeing and have been fleeing long enough, escape
                self.msg("|y$You() successfully $conj(flee) from combat.|n", combatant=combatant)
                self.remove_combatant(combatant)

        # check if one side won the battle
        if not self.combatants:
            # noone left in combat - maybe they killed each other or all fled
            surviving_combatant = None
            allies, enemies = (), ()
        else:
            # grab a random survivor and check of they have any living enemies.
            surviving_combatant = random.choice(list(self.combatants.keys()))
            allies, enemies = self.get_sides(surviving_combatant)

        if not enemies:
            # if one way or another, there are no more enemies to fight
            still_standing = list_to_string(f"$You({comb.key})" for comb in allies)
            knocked_out = list_to_string(comb for comb in self.defeated_combatants if comb.hp > 0)
            killed = list_to_string(comb for comb in self.defeated_combatants if comb.hp <= 0)

            if still_standing:
                txt = [f"The combat is over. {still_standing} are still standing."]
            else:
                txt = ["The combat is over. No-one stands as the victor."]
            if knocked_out:
                txt.append(f"{knocked_out} were taken down, but will live.")
            if killed:
                txt.append(f"{killed} were killed.")
            self.msg(txt)
            self.stop_combat()

    def at_repeat(self, **kwargs):
        """
        This is called every time the script ticks (how fast depends on if this handler runs a
        twitch- or turn-based combat).
        """
        self.execute_full_turn()


def get_or_create_combathandler(location, combat_tick=3, combathandler_name="combathandler"):
    """
    Joins or continues combat. This is a access function that will either get the
    combathandler on the current room or create a new one.

    Args:
        location (EvAdventureRoom): Where to start the combat.
        combat_tick (int): How often (in seconds) the combathandler will perform a tick. The
            shorter this interval, the more 'twitch-like' the combat will be. E.g.
        combathandler_name (str): If the combathandler should be stored with a different script
            name. Changing this could allow multiple combats to coexist in the same location.

    Returns:
        CombatHandler: The new or created combathandler.

    Notes:
        The combathandler starts disabled; one needs to run `.start` on it once all
        (initial) combatants are added.

    """
    if not location:
        raise CombatFailure("Cannot start combat without a location.")

    combathandler = location.scripts.get(combathandler_name).first()
    if not combathandler:
        combathandler = create_script(
            EvAdventureCombatHandler,
            key=combathandler_name,
            obj=location,
            interval=combat_tick,
            persistent=True,
            autostart=False,
        )
    return combathandler


# ------------------------------------------------------------
#
# Tick-based fast combat (Diku-style)
#
#   To use, add `CmdCombat` (only) to CharacterCmdset, then
#   attack a target
#
# ------------------------------------------------------------

_COMBAT_HELP = """|rYou are in combat!|n

Examples of commands:

    - |yhit/attack <target>|n   - strike, hit or smite your foe with your current weapon or spell
    - |ywield <item>|n          - wield a weapon, shield or spell rune, swapping old with new

    - |yboost STR of <recipient> vs <target>|n   - give an ally advantage on their next STR action
    - |yboost INT vs <target>|n                  - give yourself advantage on your next INT action
    - |yfoil DEX of <recipient> vs <target>|n    - give an enemy disadvantage on their next DEX action

    - |yuse <item>|n                             - use/consume an item in your inventory
    - |yuse <item> on <target>|n                 - use an item on an enemy or ally

    - |yhold|n                                   - hold your attack, doing nothing
    - |yflee|n                                   - start to flee or disengage from combat

Use |yhelp <command>|n for more info. Use |yhelp combat|n to re-show this list."""


class _CmdCombatBase(Command):
    """
    Base combat class for combat. Change the combat-tick to determine
    how quickly the combat will 'tick'.

    """

    combathandler_name = "combathandler"
    combat_tick = 3
    flee_timeout = 5

    @property
    def combathandler(self):
        combathandler = getattr(self, "_combathandler", None)
        if not combathandler:
            self._combathandler = combathandler = get_or_create_combathandler(
                self.caller.location, combat_tick=self.combat_tick
            )
        return combathandler

    def parse(self):
        super().parse()

        self.args = self.args.strip()

        if not self.caller.location or not self.caller.location.allow_combat:
            self.msg("Can't fight here!")
            raise InterruptCommand()


class TwitchCombatCmdSet(CmdSet):
    """
    Commandset added when calling the attack command, starting the combat.

    """

    name = "Twitchcombat cmdset"
    priority = 1
    mergetype = "Union"  # use Replace to lock down all other commands
    no_exits = True  # don't allow combatants to walk away

    def at_cmdset_creation(self):
        self.add(CmdTwitchAttack())
        self.add(CmdLook())
        self.add(CmdHelpCombat())
        self.add(CmdHold())
        self.add(CmdStunt())
        self.add(CmdUseItem())
        self.add(CmdWield())
        self.add(CmdFlee())


class CmdTwitchAttack(_CmdCombatBase):
    """
    Start or join a fight. Your attack will be using the Ability relevent for your current weapon
    (STR for melee, WIS for ranged attacks, INT for magic)

    Usage:
      attack <target>
      hit <target>

    """

    key = "attack"
    aliases = ("hit", "twitch combat")
    help_category = "combat"

    def parse(self):
        super().parse()
        self.args = self.args.strip()

    def func(self):
        if not self.args:
            self.msg("What are you attacking?")
            return

        target = self.caller.search(self.args)
        if not target:
            return

        if not hasattr(target, "hp"):
            self.msg(f"You can't attack that.")
            return
        elif target.hp <= 0:
            self.msg(f"{target.get_display_name(self.caller)} is already down.")
            return

        if target.is_pc and not target.location.allow_pvp:
            self.msg("PvP combat is not allowed here!")
            return

        # add combatants to combathandler. this can be done safely over and over
        is_new = self.combathandler.add_combatant(self.caller)
        if is_new:
            # just joined combat - add the combat cmdset
            self.caller.cmdset.add(TwitchCombatCmdSet, persistent=True)
            self.msg(_COMBAT_HELP)

        is_new = self.combathandler.add_combatant(target)
        if is_new and target.is_pc:
            # a pvp battle
            target.cmdset.add(TwitchCombatCmdSet, persistent=True)
            target.msg(_COMBAT_HELP)

        self.combathandler.queue_action(self.caller, {"key": "attack", "target": target})
        self.combathandler.start_combat()
        self.msg(f"You attack {target.get_display_name(self.caller)}!")


class CmdLook(default_cmds.CmdLook):
    def func(self):
        if not self.args:
            combathandler = get_or_create_combathandler(self.caller.location)
            txt = str(combathandler.get_combat_summary(self.caller))
            maxwidth = max(display_len(line) for line in txt.strip().split("\n"))
            self.msg(f"|r{pad(' Combat Status ', width=maxwidth, fillchar='-')}|n\n{txt}")
        else:
            # use regular look to look at things
            super().func()


class CmdHelpCombat(_CmdCombatBase):
    """
    Re-show the combat command summary.

    Usage:
      help combat

    """

    key = "help combat"

    def func(self):
        self.msg(_COMBAT_HELP)


class CmdHold(_CmdCombatBase):
    """
    Hold back your blows, doing nothing.

    Usage:
        hold

    """

    key = "hold"

    def func(self):
        self.combathandler.queue_action(self.caller, {"key": "hold"})
        self.msg("You hold, doing nothing.")


class CmdStunt(_CmdCombatBase):
    """
    Perform a combat stunt, that boosts an ally against a target, or
    foils an enemy, giving them disadvantage against an ally.

    Usage:
        boost [ability] <recipient> <target>
        foil [ability] <recipient> <target>
        boost [ability] <target>       (same as boost me <target>)
        foil [ability] <target>        (same as foil <target> me)

    Example:
        boost STR me Goblin
        boost DEX Goblin
        foil STR Goblin me
        foil INT Goblin
        boost INT Wizard Goblin

    """

    key = "stunt"
    aliases = (
        "boost",
        "foil",
    )
    help_category = "combat"

    def parse(self):
        super().parse()
        args = self.args

        if not args or " " not in args:
            self.msg("Usage: <ability> [of] <recipient> [vs] <target>")
            raise InterruptCommand()

        advantage = self.cmdname != "foil"

        # extract data from the input

        stunt_type, recipient, target = None, None, None

        stunt_type, *args = args.split(None, 1)
        args = args[0] if args else ""

        recipient, *args = args.split(None, 1)
        target = args[0] if args else None

        # validate input and try to guess if not given

        # ability is requried
        if stunt_type.strip() not in ABILITY_REVERSE_MAP:
            self.msg("That's not a valid ability.")
            raise InterruptCommand()

        if not recipient:
            self.msg("Must give at least a recipient or target.")
            raise InterruptCommand()

        if not target:
            # something like `boost str target`
            target = recipient if advantage else "me"
            recipient = "me" if advantage else recipient

        # if we still have None:s at this point, we can't continue
        if None in (stunt_type, recipient, target):
            self.msg("Both ability, recipient and  target of stunt must be given.")
            raise InterruptCommand()

        # save what we found so it can be accessed from func()
        self.advantage = advantage
        self.stunt_type = ABILITY_REVERSE_MAP[stunt_type.strip()]
        self.recipient = recipient.strip()
        self.target = target.strip()

    def func(self):

        combathandler = self.combathandler
        target = self.caller.search(self.target, candidates=combathandler.combatants.keys())
        if not target:
            return
        recipient = self.caller.search(self.recipient, candidates=combathandler.combatants.keys())
        if not recipient:
            return

        self.combathandler.queue_action(
            self.caller,
            {
                "key": "stunt",
                "recipient": recipient,
                "target": target,
                "advantage": self.advantage,
                "stunt_type": self.stunt_type,
                "defense_type": self.stunt_type,
            },
        )
        self.msg("You prepare a stunt!")


class CmdUseItem(_CmdCombatBase):
    """
    Use an item in combat. The item must be in your inventory to use.

    Usage:
        use <item>
        use <item> [on] <target>

    Examples:
        use potion
        use throwing knife on goblin
        use bomb goblin

    """

    key = "use"
    help_category = "combat"

    def parse(self):
        super().parse()
        args = self.args

        if not args:
            self.msg("What do you want to use?")
            raise InterruptCommand()
        elif "on" in args:
            self.item, self.target = (part.strip() for part in args.split("on", 1))
        else:
            self.item, *target = args.split(None, 1)
            self.target = target[0] if target else "me"

    def func(self):

        item = self.caller.search(
            self.item, candidates=self.caller.equipment.get_usable_objects_from_backpack()
        )
        if not item:
            self.msg("(You must carry the item to use it.)")
            return
        if self.target:
            target = self.caller.search(self.target)
            if not target:
                return

        self.combathandler.queue_action(
            self.caller, {"key": "use", "item": item, "target": self.target}
        )
        self.msg(f"You prepare to use {item.get_display_name(self.caller)}!")


class CmdWield(_CmdCombatBase):
    """
    Wield a weapon or spell-rune. You will the wield the item, swapping with any other item(s) you
    were wielded before.

    Usage:
      wield <weapon or spell>

    Examples:
      wield sword
      wield shield
      wield fireball

    Note that wielding a shield will not replace the sword in your hand, while wielding a two-handed
    weapon (or a spell-rune) will take two hands and swap out what you were carrying.

    """

    key = "wield"
    help_category = "combat"

    def parse(self):
        if not self.args:
            self.msg("What do you want to wield?")
            raise InterruptCommand()
        super().parse()

    def func(self):

        item = self.caller.search(
            self.args, candidates=self.caller.equipment.get_wieldable_objects_from_backpack()
        )
        if not item:
            self.msg("(You must carry the item to wield it.)")
            return
        self.combathandler.queue_action(self.caller, {"key": "wield", "item": item})
        self.msg(f"You start wielding {item.get_display_name(self.caller)}!")


class CmdFlee(_CmdCombatBase):
    """
    Flee or disengage from combat. An opponent may attempt a 'hinder' action to stop you
    with a DEX challenge.

    Usage:
      flee

    """

    key = "flee"
    aliases = ["disengage"]
    help_category = "combat"

    def func(self):
        self.combathandler.queue_action(self.caller, {"key": "flee"})
        self.msg("You prepare to flee!")


class TwitchAttackCmdSet(CmdSet):
    """
    For quickly adding only the attack command to yourself.
    """

    def at_cmdset_creation(self):
        self.add(CmdTwitchAttack())


# -----------------------------------------------------------------------------------
#
# Turn-based combat (Final Fantasy style), using a menu
#
# Activate by adding the CmdTurnCombat command to Character cmdset, then
# use it to attack a target.
#
# -----------------------------------------------------------------------------------


def _get_combathandler(caller):
    evmenu = caller.ndb._evmenu
    if not hasattr(evmenu, "combathandler"):
        evmenu.combathandler = get_or_create_combathandler(caller.location)
    return evmenu.combathandler


def _queue_action(caller, raw_string, **kwargs):
    action_dict = kwargs["action_dict"]
    _get_combathandler(caller).queue_action(caller, action_dict)
    return "node_wait"


def _step_wizard(caller, raw_string, **kwargs):
    """
    Many options requires stepping through several steps, wizard style. This
    will redirect back/forth in the sequence.

    E.g. Stunt boost -> Choose ability to boost -> Choose recipient -> Choose target -> queue

    """
    steps = kwargs.get("steps", [])
    nsteps = len(steps)
    istep = kwargs.get("istep", 0)
    # one of abort, back, forward
    step_direction = kwargs.get("step", "forward")

    match step_direction:
        case "abort":
            # abort this wizard, back to top-level combat menu, dropping changes
            return "node_combat"
        case "back":
            # step back in wizard
            istep = kwargs["istep"] = max(0, istep - 1)
            return steps[istep], kwargs
        case _:
            # forward (default)
            if istep >= nsteps - 1:
                # we are already at end of wizard - queue action!
                return _queue_action(caller, raw_string, **kwargs)
            else:
                # step forward
                istep = kwargs["istep"] = min(nsteps - 1, istep + 1)
                return steps[istep], kwargs


def _get_default_wizard_options(caller, **kwargs):
    """
    Get the standard wizard options for moving back/forward/abort. This can be extended to the end
    of other options.

    """

    return [
        {"key": ("back", "b"), "goto": (_step_wizard, {**kwargs, **{"step": "back"}})},
        {"key": ("abort", "a"), "goto": (_step_wizard, {**kwargs, **{"step": "abort"}})},
    ]


def node_choose_enemy_target(caller, raw_string, **kwargs):
    """
    Choose an enemy as a target for an action
    """
    texts = "Choose a target."
    action_dict = kwargs["action_dict"]

    combathandler = _get_combathandler(caller)
    _, enemies = combathandler.get_sides(caller)

    options = [
        {
            "desc": target.get_display_name(caller),
            "goto": (_step_wizard, {"action_dict": {**action_dict, **{"target": target}}}),
        }
        for target in enemies
    ]
    options.extend(_get_default_wizard_options(caller, **kwargs))
    return text, options


def node_choose_allied_target(caller, raw_string, **kwargs):
    """
    Choose an enemy as a target for an action
    """
    texts = "Choose a target."
    action_dict = kwargs["action_dict"]

    combathandler = _get_combathandler(caller)
    allies, _ = combathandler.get_sides(caller)

    # can choose yourself
    options = [
        {
            "desc": "Yourself",
            "goto": (
                _step_wizard,
                {"action_dict": {**action_dict, **{"target": caller, "recipient": caller}}},
            ),
        }
    ]
    options.extend(
        [
            {
                "desc": target.get_display_name(caller),
                "goto": (
                    _step_wizard,
                    {"action_dict": {**action_dict, **{"target": target, "recipient": target}}},
                ),
            }
            for target in allies
        ]
    )
    options.extend(_get_default_wizard_options(caller, **kwargs))
    return text, options


def node_choose_ability(caller, raw_string, **kwargs):
    """
    Select an ability to use/boost etc.
    """
    text = "Choose the ability to apply"
    action_dict = kwargs["action_dict"]

    options = [
        {
            "desc": abi.value,
            "goto": (
                _step_wizard,
                {"action_dict": {**action_dict, **{"stunt_type": abi, "defense_type": abi}}},
            ),
        }
        for abiname, abi in (
            Ability.STR,
            Ability.DEX,
            Ability.CON,
            Ability.INT,
            Ability.INT,
            Ability.WIS,
            Ability.CHA,
        )
    ]
    options.extend(_get_default_wizard_options(caller, **kwargs))
    return text, options


def node_choose_use_item(caller, raw_string, **kwargs):
    """
    Choose item to use.

    """
    text = "Select the item"
    action_dict = kwargs["action_dict"]

    options = [
        {
            "desc": item.get_display_name(caller),
            "goto": (_step_wizard, {**action_dict, **{"item": item}}),
        }
        for item in self.caller.equipment.get_usable_objects_from_backpack()
    ]
    options.extend(_get_default_wizard_options(caller, **kwargs))
    return text, options


def node_choose_wield_item(caller, raw_string, **kwargs):
    """
    Choose item to use.

    """
    text = "Select the item"
    action_dict = kwargs["action_dict"]

    options = [
        {
            "desc": item.get_display_name(caller),
            "goto": (_step_wizard, {**action_dict, **{"item": item}}),
        }
        for item in self.caller.equipment.get_wieldable_objects_from_backpack()
    ]
    options.extend(_get_default_wizard_options(caller, **kwargs))
    return text, options


def node_combat(caller, raw_string, **kwargs):
    """Base combat menu"""

    combathandler = _get_combathandler(caller)

    text = combathandler.get_combat_summary(caller)
    options = [
        {
            "desc": "attack an enemy",
            "goto": (
                _step_wizard,
                {
                    "steps": ["node_choose_enemy_target"],
                    "action_dict": {"key": "attack", "target": None},
                },
            ),
        },
        {
            "desc": "Stunt - gain a later advantage against a target",
            "goto": (
                _step_wizard,
                {
                    "steps": [
                        "node_choose_ability",
                        "node_choose_allied_target",
                        "node_choose_enemy_target",
                    ],
                    "action_dict": {"key": "stunt", "advantage": True},
                },
            ),
        },
        {
            "desc": "Stunt - give an enemy disadvantage against yourself or an ally",
            "goto": (
                _step_wizard,
                {
                    "steps": [
                        "node_choose_ability",
                        "node_choose_enemy_target",
                        "node_choose_allied_target",
                    ],
                    "action_dict": {"key": "stunt", "advantage": False},
                },
            ),
        },
        {
            "desc": "Use an item on yourself or an ally",
            "goto": (
                _step_wizard,
                {
                    "steps": ["node_choose_item", "node_choose_allied_target"],
                    "action_dict": {"key": "use", "item": None, "target": None},
                },
            ),
        },
        {
            "desc": "Use an item on an enemy",
            "goto": (
                _step_wizard,
                {
                    "steps": ["node_choose_use_item", "node_choose_enemy_target"],
                    "action_dict": {"key": "use", "item": None, "target": None},
                },
            ),
        },
        {
            "desc": "Wield/swap with an item from inventory",
            "goto": (
                _step_wizard,
                {
                    "steps": ["node_choose_wield_item"],
                    "action_dict": {"key": "wield", "item": None},
                },
            ),
        },
        {
            "desc": "flee!",
            "goto": (_queue_action, {"flee": {"key": "flee"}}),
        },
        {
            "desc": "hold, doing nothing",
            "goto": (_queue_action, {"action_dict": {"key": "hold"}}),
        },
    ]

    return text, options


# Add this command to the Character cmdset to make turn-based combat available.


class _CmdTurnCombatBase(_CmdCombatBase):
    """
    Base combat class for combat. Change the combat-tick to determine
    how quickly the combat will 'tick'.

    """

    combathandler_name = "combathandler"
    combat_tick = 30
    flee_timeout = 2


class CmdTurnAttack(_CmdTurnCombatBase):
    """
    Start or join combat.

    Usage:
      attack [<target>]

    """

    key = "attack"
    aliases = ["hit", "turnbased combat"]

    def parse(self):
        super().parse()
        self.args = self.args.strip()

    def func(self):

        if not self.args:
            self.msg("What are you attacking?")
            return

        target = self.caller.search(self.args)
        if not target:
            return

        if not hasattr(target, "hp"):
            self.msg(f"You can't attack that.")
            return
        elif target.hp <= 0:
            self.msg(f"{target.get_display_name(self.caller)} is already down.")
            return

        if target.is_pc and not target.location.allow_pvp:
            self.msg("PvP combat is not allowed here!")
            return

        # add combatants to combathandler. this can be done safely over and over
        self.combathandler.add_combatant(self.caller)
        self.combathandler.add_combatant(target)
        self.combathandler.start_combat()

        # build and start the menu
        evmenu.EvMenu(
            self.caller,
            {
                "node_choose_enemy_target": node_choose_enemy_target,
                "node_choose_allied_target": node_choose_allied_target,
                "node_choose_ability": node_choose_ability,
                "node_choose_use_item": node_choose_use_item,
                "node_choose_wield_item": node_choose_wield_item,
                "node_combat": node_combat,
            },
            startnode="node_combat",
            combathandler=self.combathandler,
            cmdset_mergetype="Union",
        )


class TurnAttackCmdSet(CmdSet):
    """
    CmdSet for the turn-based combat.
    """

    def at_cmdset_creation(self):
        self.add(CmdTurnAttack())