from ansible.module_utils.policy_communicator import PolicyCommunicator
from ansible.module_utils.policy_communicator import DuplicateRequestError

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils._text import to_native

from abc import ABC, abstractmethod

import time
import json

import inspect

# Add all the base resources that can be configured in the
# Policy API here. Required to infer base resource params.
BASE_RESOURCES = {"NSXTSegment", "NSXTTier0"}


class NSXTBaseRealizableResource(ABC):

    INCORRECT_ARGUMENT_NAME_VALUE = "error_invalid_parameter"

    def realize(self, supports_check_mode=True,
                successful_resource_exec_logs=[]):
        # must call this method to realize the creation, update, or deletion of
        # resource
        self.resource_class = self.__class__

        if not hasattr(self, "_arg_spec"):
            self._make_ansible_arg_spec()

        self.module = AnsibleModule(argument_spec=self._arg_spec,
                                    supports_check_mode=supports_check_mode)

        # Infer manager credentials
        mgr_hostname = self.module.params['hostname']
        mgr_username = self.module.params['username']
        mgr_password = self.module.params['password']

        # Each manager has an associated PolicyCommunicator
        self.policy_communicator = PolicyCommunicator.get_instance(
            mgr_username, mgr_hostname, mgr_password)

        self.validate_certs = self.module.params['validate_certs']
        self._state = self._getAttribute('state')
        self.id = self._getAttribute('id')

        # Extract the resource params from module
        self.resource_params = self._extract_resource_params(
            self.module.params.copy())

        # parent_info is passed to subresources of a resource automatically
        if not hasattr(self, "_parent_info"):
            self._parent_info = {}
        self.update_parent_info(self._parent_info)
        self._update_parent_info()

        try:
            # get existing resource schema
            _, self.existing_resource = self._send_request_to_API(
                "/" + self.id, ignore_error=False)
            # As Policy API's PATCH requires all attributes to be filled,
            # we fill the missing resource params (the params not specified)
            # by user using the existing params
            self._fill_missing_resource_params(
                self.existing_resource, self.resource_params)
        except Exception as err:
            # the resource does not exist currently on the manager
            self.existing_resource = None

        self._achieve_state(successful_resource_exec_logs)

    def get_unique_arg_identifier(self):
        # Can be overriden in the subclass to provide different
        # unique_arg_identifier. It is used to infer which args belong to which
        # subresource.
        # By default, class name is used.
        return self.get_resource_name()

    def get_state(self):
        return self._state

    @staticmethod
    @abstractmethod
    def get_resource_base_url(parent_info):
        # Must be overridden by the subclass
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_resource_spec():
        # Must be overridden by the subclass
        raise NotImplementedError

    def get_resource_name(self):
        return self.__class__.__name__

    def create_or_update_subresource_first(self):
        # return True if subresource should be created/updated before parent
        # resource
        return False

    def delete_subresource_first(self):
        # return True if subresource should be deleted before parent resource
        return True

    def achieve_subresource_state_if_del_parent(self):
        # return True if the resource is to be realized with its own specified
        # state irrespective of the state of its parent resource.
        return False

    def do_wait_till_create(self):
        # By default, we do not wait for the parent resource to be created or
        # updated before its subresource is to be realized.
        return False

    @staticmethod
    def get_resource_creation_priority():
        # this priority can be used to create/delete subresources
        # at the same level in a particular order.
        # by default, it returns 1 so the resources are created/updated/
        # deleted in a fixed but random order.
        # should be overloaded in subclass to specify its priority.
        # for creation or update, we iterate in descending order.
        # for deletion, we iterate in ascending order.
        return 1

    def achieve_subresource_state(self, successful_resource_exec_logs):
        """
            Achieve the state of each sub-resource.
        """
        for sub_resource_class in self._get_sub_resources_class_of(
                self.resource_class):
            sub_resource = sub_resource_class()
            sub_resource._arg_spec = self._arg_spec
            sub_resource._parent_info = self._parent_info
            sub_resource.realize(successful_resource_exec_logs)

    def update_resource_params(self):
        # Can be used to updates the params of resource before making
        # the API call.
        # Should be overridden in the subclass if needed
        pass

    def check_for_update(self, existing_params, resource_params):
        """
            resource_params: dict
            existing_params: dict

            Compares the existing_params with resource_params and returns
            True if they are different. At a base level, it traverses the
            params and matches one-to-one. If the value to be matched is a
            - dict, it traverses that also.
            - list, it merely compares the order.
            Can be overriden in the subclass for specific custom checking.
        """
        if not existing_params:
            return False
        for k, v in resource_params.items():
            if k not in existing_params:
                return True
            elif type(v).__name__ == 'dict':
                if self.check_for_update(existing_params[k], v):
                    return True
            elif v != existing_params[k]:
                return True
        return False

    def update_parent_info(self, parent_info):
        # Override this and fill in self._parent_info if that is to be passed
        # to the sub-resource
        # By default, parent's id is passed
        parent_info[self.get_unique_arg_identifier() + "_id"] = self.id

    # ----------------------- Private Methods Begin -----------------------
    def _update_parent_info(self):
        # This update is always performed and should not be overriden by the
        # subresource's class
        self._parent_info["_parent"] = self

    def _make_ansible_arg_spec(self):
        """
            We read the arg_spec of all the resources associated that
            are associated with this resource and create one complete
            arg_spec.
        """
        if self.get_resource_name() in BASE_RESOURCES:
            self._arg_spec = {}
            # Update it with VMware arg spec
            self._arg_spec.update(
                PolicyCommunicator.get_vmware_argument_spec())

            # Update with all sub-resources arg spec
            for sub_resources_class in self._get_sub_resources_class_of(
                    self.resource_class):
                self._update_arg_spec_with_all_resources(sub_resources_class)

            # Make all subresources args not required...
            for arg, spec in self._arg_spec.items():
                spec["required"] = False

            # ... then update it with top most resource spec ..
            self._update_arg_spec_with_resource(self.resource_class)

            # ... then create a local Ansible Module ...
            module = AnsibleModule(argument_spec=self._arg_spec)

            # ... then infer which subresources are specified by the user and
            # update their arg_spec with appropriate `required` fields.
            for sub_resources_class in self._get_sub_resources_class_of(
                    self.__class__):
                self._update_req_arg_spec_of_specified_resource(
                    sub_resources_class, module)

    def _update_req_arg_spec_of_specified_resource(self, resource_class,
                                                   ansible_module):
        # If the resource identified by resource_class is specified by the
        # user, this method updates the _arg_spec with the resources
        # arg_spec
        if (ansible_module.params[resource_class.get_unique_arg_identifier() +
                                  "_id"]) is not None:
            # This resource is specified so update the `required` fields of
            # this resource.
            resource_arg_spec = resource_class.get_resource_spec()
            for key, value in resource_arg_spec.items():
                arg_key = (resource_class.get_unique_arg_identifier() + "_" +
                           key)
                self._arg_spec[arg_key]["required"] = resource_arg_spec[
                    key].get("required", False)
            base_arg_spec = self._get_base_arg_spec_of_resource()
            for key, value in base_arg_spec.items():
                arg_key = (resource_class.get_unique_arg_identifier() + "_" +
                           key)
                self._arg_spec[arg_key]["required"] = base_arg_spec[
                    key].get("required", False)
        # Do this for all the subresources of this resource also
        for sub_resources_class in self._get_sub_resources_class_of(
                resource_class):
            self._update_req_arg_spec_of_specified_resource(
                sub_resources_class, ansible_module)

    def _update_arg_spec_with_resource(self, resource_class):
        # updates _arg_spec with resource_class's arg_spec
        resource_arg_spec = self._get_base_arg_spec_of_resource()
        resource_arg_spec.update(resource_class.get_resource_spec())
        self._update_resource_arg_spec_with_arg_identifier(resource_arg_spec,
                                                           resource_class)
        self._arg_spec.update(resource_arg_spec)

    def _update_arg_spec_with_all_resources(self, resource_class):
        # updates _arg_spec with resource_class's arg_spec and all it's
        # sub-resources
        self._update_arg_spec_with_resource(resource_class)
        # go to each child of resource_class and update it
        for sub_resources_class in self._get_sub_resources_class_of(
                resource_class):
            self._update_arg_spec_with_all_resources(sub_resources_class)

    def _update_resource_arg_spec_with_arg_identifier(self, resource_arg_spec,
                                                      resource_class):
        # update the arg_spec of resource with class resource_class in
        # self._arg_spec prepending the unique_arg_identifier of resource
        # to the keys in arg_spec of resource
        if resource_class is None:
            return
        if resource_class.__name__ in BASE_RESOURCES:
            return
        arg_spec = {}
        for key, value in resource_arg_spec.items():
            key = resource_class.get_unique_arg_identifier() + "_" + key
            arg_spec[key] = value
        resource_arg_spec.clear()
        resource_arg_spec.update(arg_spec)

    def _get_base_arg_spec_of_resource(self):
        resource_base_arg_spec = {}
        resource_base_arg_spec.update(
            # these are the base args for any NSXT Resource
            id=dict(
                required=True,
                type='str'
            ),
            display_name=dict(
                required=True,
                type='str'
            ),
            description=dict(
                required=False,
                type='str'
            ),
            tags=dict(
                required=False,
                type=list,
                options=dict(
                    scope=dict(
                        required=True,
                        type='str'
                    ),
                    tag=dict(
                        required=True,
                        type='str'
                    )
                )
            ),
            state=dict(
                required=True,
                choices=['present', 'absent']
            )
        )
        return resource_base_arg_spec

    def _getAttribute(self, attribute):
        """
            attribute: String

            Returns the attribute from module params if specified.
            - If it's a sub-resource, the param name must have its
              unique_arg_identifier as a prefix.
            - There is no prefix for base resource.
        """
        if self.get_resource_name() in BASE_RESOURCES:
            return self.module.params.get(
                attribute, self.module.params.get(
                    self.get_resource_name() + "_" + attribute,
                    self.INCORRECT_ARGUMENT_NAME_VALUE))
        else:
            if attribute == "state":
                # if parent has absent state, subresources should have absent
                # state if . So, irrespective of what user specifies, if parent
                # is to be deleted, the child resources will be deleted.
                # override achieve_subresource_state_if_del_parent
                # in resource class to change this behabior
                if (self._parent_info["_parent"].get_state() == "absent" and
                        not self.achieve_subresource_state_if_del_parent()):
                    return "absent"
            return self.module.params.get(
                self.get_unique_arg_identifier() + "_" + attribute,
                self.INCORRECT_ARGUMENT_NAME_VALUE)

    def _extract_resource_params(self, args=None):
        # extract the params belonging to this resource only.
        unwanted_resource_params = ["state", "id"]
        if self.get_resource_name() not in BASE_RESOURCES:
            unwanted_resource_params = set([self.get_unique_arg_identifier() +
                                           "_" + unwanted_resource_param for
                                            unwanted_resource_param in
                                            unwanted_resource_params])
        params = {}

        def filter_with_spec(spec):
            for key in spec.keys():
                arg_key = key
                if self.get_resource_name() not in BASE_RESOURCES:
                    arg_key = self.get_unique_arg_identifier() + "_" + key
                if arg_key in self.module.params and \
                    arg_key not in unwanted_resource_params and \
                        self.module.params[arg_key] is not None:
                    params[key] = self.module.params[arg_key]
        filter_with_spec(self.get_resource_spec())
        filter_with_spec(self._get_base_arg_spec_of_resource())
        return params

    def _achieve_present_state(self, successful_resource_exec_logs):
        self.update_resource_params()
        is_resource_updated = self.check_for_update(
            self.existing_resource, self.resource_params)

        if not is_resource_updated:
            # Either the resource does not exist or it exists but was not
            # updated in the YAML.
            if self.module.check_mode:
                successful_resource_exec_logs.append({
                    self.id: {
                        "changed": True,
                        "debug_out": str(json.dumps(self.resource_params)),
                        "id": '12345',
                        "resource_type": self.get_resource_name()
                    }
                })
                return
            try:
                if self.existing_resource:
                    # Resource already exists
                    successful_resource_exec_logs.append({
                        self.id: {
                            "changed": False,
                            "id": self.id,
                            "message": "%s with id %s already exists." %
                            (self.get_resource_name(), self.id),
                            "resource_type": self.get_resource_name()
                        }
                    })
                    return
                # Create a new resource
                resp = self._send_request_to_API(suffix="/" + self.id,
                                                 method='PATCH',
                                                 data=self.resource_params)

                if self.do_wait_till_create() and not self._wait_till_create():
                    raise Exception

                successful_resource_exec_logs.append({
                    self.id: {
                        "changed": True,
                        "id": self.id,
                        "body": str(resp),
                        "message": "%s with id %s created." %
                        (self.get_resource_name(), self.id),
                        "resource_type": self.get_resource_name()
                    }
                })
            except Exception as err:
                srel = successful_resource_exec_logs
                self.module.fail_json(msg="Failed to add %s with id %s."
                                          "Request body [%s]. Error[%s]."
                                          % (self.get_resource_name(),
                                             self.id, self.resource_params,
                                             to_native(err)
                                             ),
                                      successfully_updated_resources=srel)
        else:
            # The resource exists and was updated in the YAML.
            if self.module.check_mode:
                successfully_updated_resources.append({
                    "changed": True,
                    "debug_out": str(json.dumps(self.resource_params)),
                    "id": self.id,
                    "resource_type": self.get_resource_name()
                })
                return
            self.resource_params['_revision'] = \
                self.existing_resource['_revision']
            try:
                resp = self._send_request_to_API(suffix="/"+self.id,
                                                 method="PATCH",
                                                 data=self.resource_params)
                successful_resource_exec_logs.append({
                    "changed": True,
                    "id": self.id,
                    "body": str(resp),
                    "message": "%s with id %s updated." %
                    (self.get_resource_name(), self.id),
                    "resource_type": self.get_resource_name()
                })
            except Exception as err:
                srel = successful_resource_exec_logs
                self.module.fail_json(msg="Failed to update %s with id %s."
                                          "Request body [%s]. Error[%s]." %
                                          (self.get_resource_name(), self.id,
                                           self.resource_params, to_native(err)
                                           ),
                                      successfully_updated_resources=srel)

    def _achieve_absent_state(self, successful_resource_exec_logs):
        if self.existing_resource is None:
            successful_resource_exec_logs.append({
                self.id: {
                    "changed": False,
                    "msg": 'No %s exist with id %s' %
                    (self.get_resource_name(), self.id),
                    "resource_type": self.get_resource_name()
                }
            })
            return
        if self.module.check_mode:
            successful_resource_exec_logs.append({
                "changed": True,
                "debug_out": str(json.dumps(self.resource_params)),
                "id": self.id,
                "resource_type": self.get_resource_name()
            })
            return
        try:
            _ = self._send_request_to_API("/" + self.id, method='DELETE')
            self._wait_till_delete()
            successful_resource_exec_logs.append({
                "changed": True,
                "id": self.id,
                "message": "%s with id %s deleted." %
                (self.get_resource_name(), self.id)
            })
        except Exception as err:
            srel = successful_resource_exec_logs
            self.module.fail_json(msg="Failed to delete %s with id %s. "
                                      "Error[%s]." % (self.get_resource_name(),
                                                      self.id, to_native(err)),
                                  successfully_updated_resources=srel)

    def _send_request_to_API(self, suffix="", ignore_error=True,
                             method='GET', data=None):
        try:
            if self:
                resource_base_url = self.resource_class.get_resource_base_url(
                    parent_info=self._parent_info)
            else:
                resource_base_url = self.resource_class.get_resource_base_url()

            (rc, resp) = self.policy_communicator.request(
                resource_base_url + suffix, validate_certs=self.validate_certs,
                ignore_errors=ignore_error, method=method, data=data)
            return (rc, resp)
        except Exception as e:
            raise e
        return (400, None)

    def _achieve_state(self, successful_resource_exec_logs=[]):
        """
            Achieves `present` or `absent` state as specified in the YAML.
        """
        if self.id == self.INCORRECT_ARGUMENT_NAME_VALUE:
            # The resource was not specified in the YAML.
            # So, no need to realize it.
            return

        if (self._state == "present" and
                self.create_or_update_subresource_first()):
            self.achieve_subresource_state(successful_resource_exec_logs)

        if self._state == "absent" and self.delete_subresource_first():
            self.achieve_subresource_state(successful_resource_exec_logs)

        if self._state == 'present':
            self._achieve_present_state(successful_resource_exec_logs)
        else:
            self._achieve_absent_state(successful_resource_exec_logs)

        if self._state == "present" and not (
                self.create_or_update_subresource_first()):
            self.achieve_subresource_state(successful_resource_exec_logs)

        if self._state == "absent" and not self.delete_subresource_first():
            self.achieve_subresource_state(successful_resource_exec_logs)

        if self.get_resource_name() in BASE_RESOURCES:
            self.module.exit_json(
                successfully_updated_resources=successful_resource_exec_logs)

    def _get_sub_resources_class_of(self, resource_class):
        subresources = []
        for attr in resource_class.__dict__.values():
            if (inspect.isclass(attr) and
                    issubclass(attr, NSXTBaseRealizableResource)):
                subresources.append(attr)
        if hasattr(self, "_state") and self._state == "present":
            subresources.sort(key=lambda subresource:
                              subresource.get_resource_creation_priority(),
                              reverse=True)
        else:
            subresources.sort(key=lambda subresource:
                              subresource.get_resource_creation_priority(),
                              reverse=False)
        for subresource in subresources:
            yield subresource

    def _wait_till_delete(self):
        """
            Periodically checks if the resource still exists on the API server
            every 10 seconds. Returns after it has been deleted.
        """
        while True:
            try:
                self._send_request_to_API("/" + self.id)
                time.sleep(10)
            except DuplicateRequestError:
                self.module.fail_json(msg='Duplicate request')
            except Exception:
                return

    def _wait_till_create(self):
        FAILED_STATES = ["failed"]
        IN_PROGRESS_STATES = ["pending", "in_progress"]
        SUCCESS_STATES = ["partial_success", "success"]
        try:
            count = 0
            while True:
                rc, resp = self._send_request_to_API("/" + self.id)
                if 'state' in resp:
                    if any(resp['state'] in progress_status for progress_status
                            in IN_PROGRESS_STATES):
                        time.sleep(10)
                        count = count + 1
                        if count == 90:
                            # Wait for max 15 minutes for host to realize
                            return False
                    elif any(resp['state'] in progress_status for
                             progress_status in SUCCESS_STATES):
                        return True
                    else:
                        # Failed State
                        return False
                else:
                    if rc != 200:
                        time.sleep(1)
                        count = count + 1
                        if count == 90:
                            # Wait for max 15 minutes for host to realize
                            return False
                    else:
                        return True
        except Exception as err:
            return False

    def _fill_missing_resource_params(self, existing_params, resource_params):
        """
            resource_params: dict
            existing_params: dict

            Fills resource_params with the key:value from existing_params if
            missing in the former.
        """
        if not existing_params:
            return
        for k, v in existing_params.items():
            if k not in resource_params:
                resource_params[k] = v
            elif type(v).__name__ == 'dict':
                self._fill_missing_resource_params(v, resource_params[k])
    # ----------------------- Private Methods End -----------------------
