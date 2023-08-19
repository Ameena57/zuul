# Copyright (C) 2020 Red Hat, Inc
# Copyright (C) 2023 Acme Gating, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
import re2


def filter_allowed_disallowed(
        subjects, allowed_patterns, disallowed_patterns):
    """Filter a list using allowed and disallowed patterns.

    :param list subjects: A list of strings to filter.
    :param allowed_patterns: A list of re2-compatible patterns to allow.
       If empty, all subjects are allowed (see next).
    :param disallowed_patterns: A list of re2-compatible patterns to
       reject.  A more-specific pattern here may override a less-specific
       allowed pattern.  If empty, all allowed subjects will pass.
    """
    ret = []
    for subject in subjects:
        allowed = True
        if allowed_patterns:
            allowed = False
            for pattern in allowed_patterns:
                if re2.match(pattern, subject):
                    allowed = True
                    break
        if allowed and disallowed_patterns:
            for pattern in disallowed_patterns:
                if re2.match(pattern, subject):
                    allowed = False
                    break
        if allowed:
            ret.append(subject)
    return ret


class ZuulRegex:
    def __init__(self, pattern, negate=False):
        self.pattern = pattern
        self.negate = negate
        # TODO: switch this to re2
        self.re = re.compile(pattern)

    def __eq__(self, other):
        return (isinstance(other, ZuulRegex) and
                self.pattern == other.pattern and
                self.negate == other.negate)

    def __ne__(self, other):
        return not self.__eq__(other)

    def match(self, subject):
        if self.negate:
            return not self.re.match(subject)
        return self.re.match(subject)

    def fullmatch(self, subject):
        if self.negate:
            return not self.re.fullmatch(subject)
        return self.re.fullmatch(subject)

    def search(self, subject):
        if self.negate:
            return not self.re.search(subject)
        return self.re.search(subject)

    def serialize(self):
        return {
            "pattern": self.pattern,
            "negate": self.negate,
        }

    @classmethod
    def deserialize(cls, data):
        o = cls(data['pattern'], data['negate'])
        return o
