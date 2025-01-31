"""
Copyright (c) Facebook, Inc. and its affiliates.
"""

import logging
import numpy as np
import time

from block_data import (
    PASSABLE_BLOCKS,
    BUILD_BLOCK_REPLACE_MAP,
    BUILD_IGNORE_BLOCKS,
    BUILD_INTERCHANGEABLE_PAIRS,
)
from build_utils import blocks_list_to_npy, npy_to_blocks_list, to_relative_pos
from entities import MOBS_BY_ID
import search
import perception
import util

# tasks should be interruptible; that is, if they
# store state, stopping the task and doing something
# else should not mess up their state and just the
# current state should be enough to do the task from
# any ob

# put a counter and a max_count so can't get stuck?
class Task(object):
    def __init__(self, featurizer=None):
        self.interrupted = False
        self.featurizer = featurizer
        self.finished = False
        self.name = None
        self.block_memory = None
        self.undone = False
        # FIXME: remove these
        self.memid = None
        self.children = []

    def featurize(self):
        if self.featurizer is not None:
            return self.featurizer(self)
        else:
            return "empty"

    def step(self, agent):
        return

    def interrupt(self):
        self.interrupted = True

    def check_finished(self):
        if self.finished:
            if self.block_memory is not None:  # possibly added by a TaskNode in memory.py
                self.block_memory.end_time = self.end_time
        return self.finished

    def __repr__(self):
        return str(type(self))


class Dance(Task):
    def __init__(self, agent, task_data, featurizer=None):
        super(Dance, self).__init__(featurizer=featurizer)
        # movement should be a Movement object from dance.py
        self.movement = task_data.get("movement")

    def step(self, agent):
        self.interrupted = False
        mv = self.movement.get_move()
        if mv is None:
            self.finished = True
            return
        agent.memory.task_stack_push(mv, parent_memid=self.memid)

    def featurize(self):
        if self.featurizer is not None:
            return self.featurizer(self)
        else:
            return "dance"


class Move(Task):
    STEP_FNS = {
        (1, 0, 0): "step_pos_x",
        (-1, 0, 0): "step_neg_x",
        (0, 1, 0): "step_pos_y",
        (0, -1, 0): "step_neg_y",
        (0, 0, 1): "step_pos_z",
        (0, 0, -1): "step_neg_z",
    }

    def __init__(self, agent, task_data, featurizer=None):
        super(Move, self).__init__(featurizer=featurizer)
        self.target = util.to_block_pos(np.array(task_data["target"]))
        self.approx = task_data.get("approx", 1)
        self.path = None
        self.replace = set()

    def step(self, agent):
        self.interrupted = False

        # replace blocks if possible
        R = self.replace.copy()
        self.replace.clear()
        for (pos, idm) in R:
            agent.set_held_item(idm)
            if agent.place_block(*pos):
                logging.info("Move: replaced {}".format((pos, idm)))
            else:
                # try again later
                self.replace.add((pos, idm))
        if len(self.replace) > 0:
            logging.info("Replace remaining: {}".format(self.replace))

        # check if finished
        if util.manhat_dist(tuple(agent.pos), self.target) <= self.approx:
            if len(self.replace) > 0:
                logging.error("Move finished with non-empty replace set: {}".format(self.replace))
            self.finished = True
            if self.memid is not None:
                locmemid = agent.memory.add_location(self.target)
                locmem = agent.memory.get_location_by_id(locmemid)
                agent.memory.update_recent_entities(mems=[locmem])
                agent.memory.add_triple(self.memid, "task_effect_", locmemid)
                chat_mem_triples = agent.memory.get_triples(
                    subj=None, pred="chat_effect_", obj=self.memid
                )
                if len(chat_mem_triples) > 0:
                    chat_memid = chat_mem_triples[0][0]
                    agent.memory.add_triple(chat_memid, "chat_effect_", locmemid)
            return

        # get path
        if self.path is None or tuple(agent.pos) != self.path[-1]:
            self.path = search.astar(agent, self.target, self.approx)
            if self.path is None:
                self.handle_no_path(agent)
                return

        # take a step on the path
        assert tuple(agent.pos) == self.path.pop()
        step = tuple(self.path[-1] - agent.pos)
        step_fn = getattr(agent, self.STEP_FNS[step])
        step_fn()

    def handle_no_path(self, agent):
        delta = self.target - agent.pos
        for vec, step_fn_name in self.STEP_FNS.items():
            if np.dot(delta, vec) > 0:
                newpos = agent.pos + vec
                x, y, z = newpos
                newpos_blocks = agent.get_blocks(x, x, y, y + 1, z, z)
                # dig if necessary
                for (bp, idm) in npy_to_blocks_list(newpos_blocks, newpos):
                    self.replace.add((bp, idm))
                    agent.dig(*bp)
                # move
                step_fn = getattr(agent, step_fn_name)
                step_fn()
                break

    def featurize(self):
        if self.featurizer is not None:
            return self.featurizer(self)
        else:
            return "Move {} {} {}".format(*self.target)

    def __repr__(self):
        return "<Move {} ±{}>".format(self.target, self.approx)


class Build(Task):
    PLACE_REACH = 3

    def __init__(self, agent, task_data, featurizer=None):
        super(Build, self).__init__(featurizer=featurizer)
        self.task_data = task_data
        self.embed = task_data.get("embed", False)
        self.schematic, _ = blocks_list_to_npy(task_data["blocks_list"])
        self.origin = task_data["origin"]
        self.verbose = task_data.get("verbose", True)
        self.relations = task_data.get("relations", [])
        self.default_behavior = task_data.get("default_behavior")
        self.force = task_data.get("force", False)
        self.block_memory = None
        self.attempts = 3 * np.ones(self.schematic.shape[:3], dtype=np.uint8)
        self.fill_message = task_data.get("fill_message", False)
        self.schematic_memid = task_data.get("schematic_memid", None)
        self.schematic_tags = task_data.get("schematic_tags", [])
        self.giving_up_message_sent = False
        self.wait = False
        self.old_blocks_list = None
        self.old_origin = None

        # modify the schematic to avoid placing certain blocks
        for bad, good in BUILD_BLOCK_REPLACE_MAP.items():
            self.schematic[self.schematic[:, :, :, 0] == bad] = good
        self.new_blocks = []  # a list of (xyz, idm) of newly placed blocks

        # snap origin to ground if bottom level has dirt blocks
        # NOTE(kavyasrinet): except for when we are rebuilding the old dirt blocks, we
        # don't want to change the origin then, hence the self.force check.
        if not self.force and not self.embed and np.isin(self.schematic[:, :, :, 0], (2, 3)).any():
            h = perception.ground_height(agent, self.origin, 0)
            self.origin[1] = h[0, 0]

    def step(self, agent):
        self.interrupted = False

        # get blocks occupying build area
        ox, oy, oz = self.origin
        sy, sz, sx, _ = self.schematic.shape
        current = agent.get_blocks(ox, ox + sx - 1, oy, oy + sy - 1, oz, oz + sz - 1)

        # save state for undo()
        if self.old_blocks_list is None:
            self.old_blocks_list = npy_to_blocks_list(current, self.origin)
            if len(self.old_blocks_list) > 0:
                self.old_origin = np.min(util.strip_idmeta(self.old_blocks_list), axis=0)

        # are we done?
        # TODO: diff ignores block meta right now because placing stairs and
        # chests in the appropriate orientation is non-trivial
        diff = (
            (current[:, :, :, 0] != self.schematic[:, :, :, 0])
            & (self.attempts > 0)
            & np.isin(current[:, :, :, 0], BUILD_IGNORE_BLOCKS, invert=True)
        )
        if self.embed:
            diff &= self.schematic[:, :, :, 0] != 0  # don't delete blocks
        for pair in BUILD_INTERCHANGEABLE_PAIRS:
            diff &= np.isin(current[:, :, :, 0], pair, invert=True) | np.isin(
                self.schematic[:, :, :, 0], pair, invert=True
            )
        if not np.any(diff):
            self.finish(agent)
            return

        # blocks that would need to be removed
        remove_mask = diff & (current[:, :, :, 0] != 0)

        # destroy any blocks in the way first
        rel_yzxs = np.argwhere(remove_mask)
        xyzs = [
            (x + self.origin[0], y + self.origin[1], z + self.origin[2]) for (y, z, x) in rel_yzxs
        ]
        if xyzs:
            logging.info("Excavating {} blocks first".format(len(xyzs)))
            agent.memory.task_stack_push(
                Destroy(agent, {"schematic": util.fill_idmeta(agent, xyzs)}),
                parent_memid=self.memid,
            )
            return

        # get next block to place
        yzx = self.get_next_target(agent, current, diff)
        idm = self.schematic[tuple(yzx)]
        current_idm = current[tuple(yzx)]

        # try placing block
        target = yzx[[2, 0, 1]] + self.origin
        logging.debug("trying to place {} @ {}".format(idm, target))
        if tuple(target) in (tuple(agent.pos), tuple(agent.pos + [0, 1, 0])):
            # can't place block where you're standing, so step out of the way
            self.step_any_dir(agent)
            return
        if util.manhat_dist(agent.pos, target) <= self.PLACE_REACH:
            # block is within reach
            assert current_idm[0] != idm[0], "current={} idm={}".format(current_idm, idm)
            if current_idm[0] != 0:
                logging.debug(
                    "removing block {} @ {} from {}".format(current_idm, target, agent.pos)
                )
                agent.dig(*target)
            if idm[0] != 0:
                agent.set_held_item(idm)
                logging.debug("placing block {} @ {} from {}".format(idm, target, agent.pos))
                x, y, z = target
                if agent.place_block(x, y, z):
                    B = agent.get_blocks(x, x, y, y, z, z)
                    if B[0, 0, 0, 0] == idm[0]:
                        agent.memory.pending_agent_placed_blocks.add((x, y, z))
                    else:
                        logging.error(
                            "failed to place block {} @ {}, but place_block returned True. \
                                Got {} instead.".format(
                                idm, target, B[0, 0, 0, :]
                            )
                        )
                    self.new_blocks.append(((x, y, z), idm))
                else:
                    logging.warn("failed to place block {} from {}".format(target, agent.pos))
            self.attempts[tuple(yzx)] -= 1
            if self.attempts[tuple(yzx)] == 0 and not self.giving_up_message_sent:
                agent.send_chat(
                    "I'm skipping a block because I can't place it. Maybe something is in the way."
                )
                self.giving_up_message_sent = True
        else:
            # too far to place; move first
            task = Move(agent, {"target": target, "approx": self.PLACE_REACH}, self.featurizer)
            agent.memory.task_stack_push(task, parent_memid=self.memid)

    def finish(self, agent):
        xyz, _ = npy_to_blocks_list(self.schematic, self.origin)[0]
        memid = agent.memory.get_block_object_ids_by_xyz(xyz)[0]
        if self.schematic_memid:
            logging.info(
                "tag_block_object_from_schematic {} {}".format(memid, self.schematic_memid)
            )
            agent.memory.tag_block_object_from_schematic(memid, self.schematic_memid)
        if self.schematic_tags:
            for pred, obj in self.schematic_tags:
                logging.info("add block_object triple {} {} {}".format(memid, pred, obj))
                agent.memory.add_triple(memid, pred, obj)
                if pred == "has_name":
                    agent.memory.tag(memid, obj)

        if self.verbose:
            agent.send_chat("I finished building this")
        if self.fill_message:
            agent.send_chat("I finished filling this")
        self.finished = True

    def get_next_target(self, agent, current, diff):
        """Return the next block that will be targeted for placing

        In order:
        1. don't build over your own body
        2. build ground-up
        3. try failed blocks again at the end
        4. build closer blocks first

        Args:
        - current: yzxb-ordered current state of the region
        - diff: a yzx-ordered boolean mask of blocks that need addressing
        """
        relpos_yzx = (agent.pos - self.origin)[[1, 2, 0]]

        diff_yzx = list(np.argwhere(diff))
        diff_yzx.sort(key=lambda yzx: util.manhat_dist(yzx, relpos_yzx))  # 4
        diff_yzx.sort(key=lambda yzx: -self.attempts[tuple(yzx)])  # 3
        diff_yzx.sort(key=lambda yzx: yzx[0])  # 2
        diff_yzx.sort(
            key=lambda yzx: tuple(yzx) in (tuple(relpos_yzx), tuple(relpos_yzx + [1, 0, 0]))
        )  # 1
        return diff_yzx[0]

    def step_any_dir(self, agent):
        px, py, pz = agent.pos
        B = agent.get_blocks(px - 1, px + 1, py - 1, py + 2, pz - 1, pz + 1)
        passable = np.isin(B[:, :, :, 0], PASSABLE_BLOCKS)
        walkable = passable[:-1, :, :] & passable[1:, :, :]  # head and feet passable
        assert walkable.shape == (3, 3, 3)
        relp = np.array([1, 1, 1])  # my pos is in the middle of the 3x3x3 cube
        for step, fn in (
            ((0, 1, 0), agent.step_pos_z),
            ((0, -1, 0), agent.step_neg_z),
            ((0, 0, 1), agent.step_pos_x),
            ((0, 0, -1), agent.step_neg_x),
            ((1, 0, 0), agent.step_pos_y),
            ((-1, 0, 0), agent.step_neg_y),
        ):
            if walkable[tuple(relp + step)]:
                fn()
                return
        raise Exception("Can't step in any dir from pos={} B={}".format(agent.pos, B))

    def undo(self, agent):
        agent.send_chat("ok I will remove it.")
        if len(self.old_blocks_list) > 0:
            agent.memory.task_stack_push(
                Build(
                    agent,
                    {
                        "blocks_list": self.old_blocks_list,
                        "origin": self.old_origin,
                        "force": True,
                        "verbose": False,
                        "embed": self.embed,
                    },
                ),
                parent_memid=self.memid,
            )
        if len(self.new_blocks) > 0:
            agent.memory.task_stack_push(
                Destroy(agent, {"schematic": self.new_blocks}), parent_memid=self.memid
            )

    def featurize(self):
        if self.featurizer is not None:
            return self.featurizer(self)
        else:  # fixme
            return "Build"

    def __repr__(self):
        return "<Build {} @ {}>".format(len(self.schematic), self.origin)


class Fill(Task):
    def __init__(self, agent, task_data, featurizer=None):
        super(Fill, self).__init__(featurizer=featurizer)
        self.schematic = task_data["schematic"]  # a list of xyz tuples
        self.block_idm = task_data.get("block_idm", (2, 0))  # default 2: grass
        self.build_task = None

    def step(self, agent):
        origin = np.min(self.schematic, axis=0)
        blocks_list = np.array([((x, y, z), self.block_idm) for (x, y, z) in self.schematic])

        self.build_task = Build(
            agent,
            {
                "blocks_list": blocks_list,
                "origin": origin,
                "force": True,
                "verbose": False,
                "embed": True,
                "fill_message": True,
            },
        )
        agent.memory.task_stack_push(self.build_task)
        self.finished = True

    def undo(self, agent):
        if self.build_task is not None:
            self.build_task.undo(agent)


class Destroy(Task):
    DIG_REACH = 3

    def __init__(self, agent, task_data, featurizer=None):
        super(Destroy, self).__init__(featurizer=featurizer)
        self.schematic = task_data["schematic"]  # list[(xyz, idm)]
        self.xyz_remaining = set(util.strip_idmeta(self.schematic))
        self.dig_message = True if "dig_message" in task_data else False

        # is it destroying a whole block object? if so, save its tags
        self.destroyed_block_object_triples = []
        mem = agent.memory.get_block_object_by_xyz(next(iter(self.xyz_remaining)))
        if mem and all(xyz in self.xyz_remaining for xyz in mem.blocks.keys()):
            for pred in ["has_tag", "has_name", "has_colour"]:
                self.destroyed_block_object_triples.extend(
                    agent.memory.get_triples(subj=mem.memid, pred=pred)
                )
            logging.info(
                "Destroying block object {} tags={}".format(
                    mem.memid, self.destroyed_block_object_triples
                )
            )

    def step(self, agent):
        self.interrupted = False
        if len(self.xyz_remaining) == 0:
            self.finished = True
            if self.dig_message:
                agent.send_chat("I finished digging this.")
            return

        target = self.get_target(agent)
        if target is None:
            logging.info("No path from {} to {}".format(agent.pos, self.xyz_remaining))
            agent.send_chat("There's no path, so I'm giving up")
            self.finished = True
            return

        if util.manhat_dist(agent.pos, target) <= self.DIG_REACH:
            agent.dig(*target)
            self.xyz_remaining.remove(target)
            return
        else:
            mv = Move(agent, {"target": target, "approx": self.DIG_REACH}, self.featurizer)
            agent.memory.task_stack_push(mv, parent_memid=self.memid)

    def get_target(self, agent):
        p = agent.pos
        for i, c in enumerate(sorted(self.xyz_remaining, key=lambda c: util.manhat_dist(p, c))):
            path = search.astar(agent, c, approx=2)
            if path is not None:
                if i > 0:
                    logging.debug("Destroy get_target wasted {} astars".format(i))
                return c

        # No path to any of the blocks
        return None

    def undo(self, agent):
        agent.send_chat("ok I will build it back.")

        # if rebuilding an object, get old object tags
        schematic_tags = [(pred, obj) for _, pred, obj in self.destroyed_block_object_triples]

        # push Build task to stack
        schematic, origin = to_relative_pos(self.schematic)
        agent.memory.task_stack_push(
            Build(
                agent,
                {
                    "blocks_list": schematic,
                    "origin": origin,
                    "force": True,
                    "verbose": False,
                    "schematic_tags": schematic_tags,
                },
            ),
            parent_memid=self.memid,
        )

    def featurize(self):
        if self.featurizer is not None:
            return self.featurizer(self)
        else:  # fixme
            return "smash"


class Undo(Task):
    def __init__(self, agent, task_data):
        super(Undo, self).__init__()
        self.to_undo_memid = task_data["memid"]

    def step(self, agent):
        old_task_mem = agent.memory.get_task_by_id(self.to_undo_memid)
        old_task_mem.task.undo(agent)
        self.finished = True

    def __repr__(self):
        return "<Undo {}>".format(self.to_undo_memid)


class Spawn(Task):
    PLACE_REACH = 3

    def __init__(self, agent, task_data):
        super(Spawn, self).__init__()
        self.object_idm = task_data["object_idm"]
        self.pos = task_data["pos"]

    def step(self, agent):
        if util.manhat_dist(agent.pos, self.pos) > self.PLACE_REACH:
            task = Move(agent, {"target": self.pos, "approx": self.PLACE_REACH})
            agent.memory.task_stack_push(task, parent_memid=self.memid)
        else:
            agent.set_held_item(self.object_idm)
            if np.equal(self.pos, agent.pos).all():
                agent.step_neg_z()
            agent.place_block(self.pos[0], self.pos[1] + 1, self.pos[2])
            time.sleep(0.1)
            agent.memory.update(agent)
            x, y, z = self.pos
            mobtype = MOBS_BY_ID[self.object_idm[1]]
            mobmem = agent.memory.get_mobs(
                spatial_range=[x - 5, x + 5, y - 5, y + 5, z - 5, z + 5],
                spawntime=[time.time() - 2, -1],
                mobtype=mobtype,
            )
            if len(mobmem) > 0:
                mobmem = mobmem[0]
                agent.memory.update_recent_entities(mems=[mobmem])
                if self.memid is not None:
                    agent.memory.add_triple(self.memid, "task_effect_", mobmem.memid)
                    # the chat_effect_ triple was already made when the task is added if there was a chat...
                    # but it points to the task memory.  link the chat to the mob memory:
                    chat_mem_triples = agent.memory.get_triples(
                        subj=None, pred="chat_effect_", obj=self.memid
                    )
                    if len(chat_mem_triples) > 0:
                        chat_memid = chat_mem_triples[0][0]
                        agent.memory.add_triple(chat_memid, "chat_effect_", mobmem.memid)
            self.finished = True


class Dig(Task):
    def __init__(self, agent, task_data):
        super(Dig, self).__init__()
        self.origin = task_data["origin"]
        self.length = task_data["length"]
        self.width = task_data["width"]
        self.depth = task_data["depth"]
        self.destroy_task = None

    def undo(self, agent):
        if self.destroy_task is not None:
            self.destroy_task.undo(agent)

    def step(self, agent):

        mx, My, mz = self.origin
        Mx = mx + (self.width - 1)
        my = My - (self.depth - 1)
        Mz = mz + (self.length - 1)

        blocks = agent.get_blocks(mx, Mx, my, My, mz, Mz)

        # if top row is above ground, make sure you are digging into the ground
        if np.isin(blocks[-1, :, :, 0], PASSABLE_BLOCKS).all():
            my -= 1

        poss = [
            (x, y, z)
            for x in range(mx, Mx + 1)
            for y in range(my, My + 1)
            for z in range(mz, Mz + 1)
        ]
        schematic = util.fill_idmeta(agent, poss)
        self.destroy_task = Destroy(agent, {"schematic": schematic, "dig_message": True})
        agent.memory.task_stack_push(self.destroy_task, parent_memid=self.memid)

        self.finished = True


class Loop(Task):
    def __init__(self, agent, task_data):
        super(Loop, self).__init__()
        self.new_tasks_fn = task_data["new_tasks_fn"]
        self.stop_condition = task_data["stop_condition"]

    def step(self, agent):
        if self.stop_condition.check():
            self.finished = True
            return
        else:
            for t in self.new_tasks_fn():
                agent.memory.task_stack_push(t, parent_memid=self.memid)
