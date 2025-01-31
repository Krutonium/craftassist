"""
Copyright (c) Facebook, Inc. and its affiliates.
"""

import random

from generate_utils import *
from templates.templates import get_template


class ActionNode:
    """This class is an Action Node that represents the "Action" in the action_tree.

    A node can have a list of child nodes (ARG_TYPES) or a list of node types, it can be.
    (CHOICES).
    generate() : is responsible for initializing the ARG_TYPES and CHOICES.
    generate_description() : Generates the natural language description.
    to_dict() : Generates the action tree recursively using the children.
    """

    ARG_TYPES = None  # a list of child node types that need to be generated
    CHOICES = None  # a list of node types that can be substituted for this node

    def __init__(self, template_key, template=None):
        self.args = None  # populated by self.generate()
        self.description = None  # populated by self.generate_description()
        if template_key != "Noop":
            self.template = get_template(template_key, self, template)
        self._dialogue_type = "human_give_command"
        self._replace = None
        self._is_dialogue = False

    def generate_description(self):
        if self.description is None:
            self.description = self._generate_description()
        return self.description

    @classmethod
    def generate(cls, action_type=None):
        if cls.ARG_TYPES:
            x = cls()
            x.args = []

            for arg in cls.ARG_TYPES:
                x.args.append(arg.generate())
            return x

        if cls.CHOICES:
            c = random.choice(action_type) if type(action_type) is list else action_type

            return c.generate()

        return cls()

    def __repr__(self):
        if self.args:
            return "<{} ({})>".format(type(self).__name__, ", ".join(map(str, self.args)))
        else:
            return "<{}>".format(type(self).__name__)

    def to_dict(self):
        """Generates the action dictionary for the sentence"""

        d = {}
        action_dict = {}

        action_description_split = [x.split() for x in self.description]

        if self.args:
            # update the tree recursively.
            for arg_type, arg in zip(self.ARG_TYPES, self.args):
                # Update the action_description for children to compute spans later
                arg._action_description = action_description_split

                arg_name = arg_type.__name__
                key = to_snake_case(arg_name)  # key name in dictionary is snake case

                # BlockObject and Mob are "reference_object" in the tree
                if arg_name in ["BlockObject", "Mob"]:
                    key = "reference_object"

                action_dict.update({key: arg.to_dict()})

        # Prune out unnecessary keys from the tree
        attributes = []
        for attr, val in self.__dict__.items():
            if (
                not attr.startswith("_")
                and val not in (None, "")
                and attr not in ["args", "description", "template", "ARG_TYPES"]
            ):
                action_dict[attr] = val
                # Spans for keys : 'has_*' and repeat_count
                if (attr.startswith("has_")) or (attr in ["repeat_count"]):
                    span = find_span(action_description_split, val)
                    action_dict[attr] = span
                    if attr.startswith("has_"):
                        attributes.append(span)
        if attributes:
            action_dict["has_attribute"] = attributes

        action_name = type(self).__name__

        # For single word commands, add a blank block_object for Copy's tree
        if (action_name == "Copy") and ("reference_object" not in action_dict):
            action_dict["reference_object"] = {}

        # Copy is represented as a 'Build' action in the tree
        if action_name == "Copy":
            action_name = "Build"

        # Assign dialogue_type for classes that are dialogues
        if self._is_dialogue:
            self._dialogue_type = action_name

        # Assign replace key
        if self._replace:
            action_dict["replace"] = True

        d["dialogue_type"] = to_snake_case(self._dialogue_type, case="upper")

        # put action as a key for all actions
        if self._dialogue_type in ["human_give_command"]:
            action_dict["action_type"] = to_snake_case(action_name, case="upper")
            d["action"] = action_dict
        else:
            d.update(action_dict)

        return d
