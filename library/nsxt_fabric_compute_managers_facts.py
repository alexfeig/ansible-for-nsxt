#!/usr/bin/env python

from __future__ import absolute_import, division, print_function
__metaclass__ = type

ANSIBLE_METADATA = {'metadata_version': '1.1',
                    'status': ['preview'],
                    'supported_by': 'community'}


DOCUMENTATION = '''TODO
author: Rahul Raghuvanshi
'''

EXAMPLES = '''
- nsxt_fabric_compute_managers_facts:
      hostname: "10.192.167.137"
      username: "admin"
      password: "Admin!23Admin"
      validate_certs: False
'''

RETURN = '''# '''

import json
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.vmware import vmware_argument_spec, request
from ansible.module_utils.urls import open_url, fetch_url
from ansible.module_utils._text import to_native
from ansible.module_utils.six.moves.urllib.error import HTTPError

def main():
  argument_spec = vmware_argument_spec()

  module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

  mgr_hostname = module.params['hostname']
  mgr_username = module.params['username']
  mgr_password = module.params['password']
  validate_certs = module.params['validate_certs']

  manager_url = 'https://{}/api/v1'.format(mgr_hostname)

  changed = False
  try:
    (rc, resp) = request(manager_url+ '/fabric/compute-managers', headers=dict(Accept='application/json'),
                    url_username=mgr_username, url_password=mgr_password, validate_certs=validate_certs, ignore_errors=True)
  except Exception as err:
    module.fail_json(msg='Error accessing fabric compute manager. Error [%s]' % (to_native(err)))

  module.exit_json(changed=changed, **resp)
if __name__ == '__main__':
	main()