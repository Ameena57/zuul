# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import os


def get_default(config, section, option, default=None, expand_user=False):
    if config.has_option(section, option):
        # Need to be ensured that we get suitable
        # type from config file by default value
        if isinstance(default, bool):
            value = config.getboolean(section, option)
        elif isinstance(default, int):
            value = config.getint(section, option)
        else:
            value = config.get(section, option)
    else:
        value = default
    if expand_user and value:
        return os.path.expanduser(value)
    return value


def any_to_bool(val):
    val = str(val)
    if val.lower() in ('1', 'on', 'yes', 'true'):
        return True
    return False
