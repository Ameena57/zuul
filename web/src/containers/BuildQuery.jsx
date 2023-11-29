// Copyright 2020 BMW Group
// Copyright 2022-2023 Acme Gating, LLC
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may
// not use this file except in compliance with the License. You may obtain
// a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
// WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
// License for the specific language governing permissions and limitations
// under the License.

import { makeQueryString } from './FilterToolbar'

function makeBuildQueryString(filters, excludeResults) {
  let queryString = makeQueryString(filters)
  let resultFilter = false
  if (filters) {
    Object.keys(filters).forEach((key) => {
      if (filters[key] === 'result') {
          resultFilter = true
        }
    })
  }
  if (excludeResults && !resultFilter) {
      queryString += '&exclude_result=SKIPPED'
  }
  queryString += '&complete=true'
  return queryString
}

export { makeBuildQueryString }
