# Copyright 2014 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Functional tests for the LTI module."""

__author__ = [
    'johncox@google.com (John Cox)'
]

import re
import yaml

from common import crypto
from controllers import sites
from modules.lti import fields
from modules.lti import lti
from modules.oeditor import oeditor
from tests.functional import actions

from google.appengine.api import app_identity
from google.appengine.api import users

# Allow access to code under test. pylint: disable-msg=protected-access


class FieldsTest(actions.TestBase):

  def setUp(self):
    super(FieldsTest, self).setUp()
    self.base = dict(fields._DEFAULTS)
    self.base.update({fields.LAUNCH_URL: fields.LAUNCH_URL + '_value'})

  def test_make_overrides_defaults_from_from_dict(self):
    expected = dict(self.base)
    from_dict = {
        fields.LAUNCH_URL: 'launch_url_override',
        fields.LTI_VERSION: 'lti_version_override',
        fields.RESOURCE_LINK_ID: 'resource_link_id_override',
    }
    expected.update(from_dict)
    self.assertEqual(expected, fields.make(from_dict))

  def test_make_raises_value_error_if_bad_fields(self):
    bad_fields = ['bad1', 'bad2']

    for field in bad_fields:
      self.assertNotIn(field, fields._ALL)

    from_dict = dict(self.base)
    from_dict.update({b: None for b in bad_fields})

    with self.assertRaisesRegexp(
        ValueError, 'bad fields: %s' % ', '.join(bad_fields)):
      fields.make(from_dict)

  def test_make_raises_value_error_if_both_launch_url_keys_present(self):
    from_dict = dict(self.base)
    from_dict.update(
        {fields.LAUNCH_URL: 'foo', fields.SECURE_LAUNCH_URL: 'bar'})

    with self.assertRaisesRegexp(ValueError, 'Cannot pass both'):
      fields.make(from_dict)

  def test_make_raises_value_error_if_neither_launch_url_key_present(self):
    with self.assertRaisesRegexp(ValueError, 'Must pass one of'):
      fields.make({})

  def test_make_raises_value_error_if_missing_fields(self):
    with self.assertRaisesRegexp(
        ValueError, 'Missing required fields: ' + fields.RESOURCE_LINK_ID):
      fields.make(self.base)

  def test_make_sets_missing_defaults_and_includes_valid_passed_fields(self):
    expected = dict(self.base)
    from_dict = {
        fields.LAUNCH_URL: fields.LAUNCH_URL + '_value',
        fields.RESOURCE_LINK_ID: fields.RESOURCE_LINK_ID + '_value',
        'custom_foo': 'custom_foo_value',
    }
    expected.update(from_dict)

    self.assertEqual(expected, fields.make(from_dict))


class LtiWebappTestBase(actions.TestBase):

  def setUp(self):
    super(LtiWebappTestBase, self).setUp()
    self.app_context = sites.get_all_courses()[0]
    self.environ = dict(self.app_context.get_environ())
    self.security_config = {'key': 'key', 'secret': 'secret'}
    self.tool_config = {
        'description': 'config_description',
        'key': 'config_key',
        'name': 'config_name',
        'secret': 'config_secret',
        'url': 'http://config_url',
        'version': lti.VERSION_1_0,
    }

  def get_tool_config_yaml(self):
    return (
        '- description: %(description)s\n'
        '  name: %(name)s\n'
        '  key: %(key)s\n'
        '  secret: %(secret)s\n'
        '  url: %(url)s\n'
        '  version: %(version)s') % self.tool_config

  def get_security_config_yaml(self):
    return '- %(key)s: %(secret)s' % self.security_config

  def set_lti_security_config(self, config_yaml=None):
    if config_yaml is None:
      config_yaml = self.get_security_config_yaml()

    self.environ[lti._CONFIG_KEY_COURSE][lti._CONFIG_KEY_LTI1] = {
        lti._CONFIG_KEY_SECURITY: config_yaml}
    mock_context = actions.MockAppContext(
        environ=self.environ, namespace=self.app_context.get_namespace_name(),
        slug=self.app_context.get_slug())
    self.swap(
        lti, '_get_key_secret_manager',
        lambda _: lti._KeySecretManager(mock_context))

  def set_lti_tool_config(self, config_yaml=None):
    if config_yaml is None:
      config_yaml = self.get_tool_config_yaml()

    self.environ[lti._CONFIG_KEY_COURSE][lti._CONFIG_KEY_LTI1] = {
        lti._CONFIG_KEY_TOOLS: config_yaml}
    mock_context = actions.MockAppContext(
        environ=self.environ, namespace=self.app_context.get_namespace_name(),
        slug=self.app_context.get_slug())
    self.swap(
        lti, '_get_launch_runtime', lambda _: lti._LaunchRuntime(mock_context))


class KeySecretManagerTest(LtiWebappTestBase):

  def setUp(self):
    super(KeySecretManagerTest, self).setUp()
    self.errors = []
    self.set_lti_security_config()
    self.manager = lti._get_key_secret_manager(self.app_context)

  def assert_no_errors(self):
    self.assertEqual([], self.errors)

  def assert_parse_error(self):
    self.assertEqual([lti._ERROR_PARSE_CONFIG_YAML], self.errors)

  def test_get_raises_assertion_error_if_parsing_fails(self):
    self.set_lti_security_config(config_yaml='-invalid-')

    with self.assertRaises(AssertionError):
      self.manager.get('anything')

  def test_get_returns_existing_config(self):
    self.assertEqual(
        lti._SecurityConfig('key', 'secret'), self.manager.get('key'))

  def test_get_returns_none_for_missing_key(self):
    self.assertIsNone(self.manager.get('missing'))


class LaunchHandlerTest(LtiWebappTestBase):

  def setUp(self):
    super(LaunchHandlerTest, self).setUp()
    self.email = 'user@example.com'
    self.external_userid = crypto.get_external_user_id(
        app_identity.get_application_id(),
        str(self.app_context.get_namespace_name()), self.email)
    self.resource_link_id = 'resource_link_id'
    self.params = {
        'name': self.tool_config['name'],
        fields.RESOURCE_LINK_ID: self.resource_link_id,
    }

  def assert_matches_form_inputs(self, expected_dict, form_inputs):
    for k, v in expected_dict.iteritems():
      self.assertEqual(v, form_inputs[k])

  def assert_base_oauth_form_inputs_look_valid(self, form_inputs):
    url_field = (
        fields.SECURE_LAUNCH_URL if self.tool_config['url'].startswith('https')
        else fields.LAUNCH_URL)

    self.assertEqual('LTI-1p0', form_inputs[fields.LTI_VERSION])
    self.assertEqual(
        'basic-lti-launch-request', form_inputs[fields.LTI_MESSAGE_TYPE])
    self.assertEqual(fields._ROLE_STUDENT, form_inputs[fields.ROLES])
    self.assertEqual(self.tool_config['url'], form_inputs[url_field])
    self.assertEqual(
        self.resource_link_id, form_inputs[fields.RESOURCE_LINK_ID])

  def assert_oauth1_signature_looks_valid(self, form_inputs):
    for oauth_param in [
        'oauth_consumer_key', 'oauth_nonce', 'oauth_signature',
        'oauth_signature_method', 'oauth_timestamp', 'oauth_version']:
      self.assertIn(oauth_param, form_inputs.keys())

    self.assertEqual('HMAC-SHA1', form_inputs['oauth_signature_method'])
    self.assertEqual('1.0', form_inputs['oauth_version'])

  def assert_tool_url_set(self, body):
    self.assertIn("action='%s'" % self.tool_config['url'], body)

  def assert_user_id_equal(self, user_id, form_params):
    self.assertEqual(user_id, form_params[fields.USER_ID])

  def assert_user_not_set(self, form_params):
    self.assertNotIn(fields.USER_ID, form_params.keys())

  def get_form_inputs(self, body):
    return dict(re.findall(
      r"input type='hidden' name='(.+)' value='(.+)'", body))

  def test_get_can_process_int_secret(self):
    self.tool_config['secret'] = 2
    self.set_lti_tool_config()
    response = self.testapp.get(lti._LAUNCH_URL, params=self.params)

    self.assertEqual(200, response.status_code)

  def test_get_returns_400_if_context_not_found(self):
    response = self.testapp.get(
        lti._LAUNCH_URL, expect_errors=True, params=self.params)

    self.assertEqual(400, response.status_code)

  def test_get_returns_400_if_name_not_set(self):
    self.set_lti_tool_config()
    self.params.pop('name')
    response = self.testapp.get(
        lti._LAUNCH_URL, expect_errors=True, params=self.params)

    self.assertEqual(400, response.status_code)

  def test_get_returns_400_if_resource_link_id_not_set(self):
    self.set_lti_tool_config()
    self.params.pop(fields.RESOURCE_LINK_ID)
    response = self.testapp.get(
        lti._LAUNCH_URL, expect_errors=True, params=self.params)

    self.assertEqual(400, response.status_code)

  def test_get_when_extra_fields_set_renders_extra_fields_in_form_inputs(self):
    context_id_value = 'context_id_value'
    context_label_value = 'context_label_value'
    extra_fields_yaml = '%s: %s\n%s: %s' % (
        fields.CONTEXT_ID, context_id_value, fields.CONTEXT_LABEL,
        context_label_value)
    self.params.update(
        {'extra_fields': fields._Serializer.dump(extra_fields_yaml)})
    self.set_lti_tool_config()
    response = self.testapp.get(lti._LAUNCH_URL, params=self.params)
    form_inputs = self.get_form_inputs(response.body)

    self.assertEqual(200, response.status_code)
    self.assert_tool_url_set(response.body)
    self.assert_matches_form_inputs(
        {fields.CONTEXT_ID: context_id_value,
         fields.CONTEXT_LABEL: context_label_value}, form_inputs)
    self.assert_base_oauth_form_inputs_look_valid(form_inputs)

  def test_get_when_insecure_launch_url_set(self):
    insecure_url = 'http://something'
    self.tool_config['url'] = insecure_url
    self.set_lti_tool_config()
    response = self.testapp.get(lti._LAUNCH_URL, params=self.params)
    form_inputs = self.get_form_inputs(response.body)

    self.assertEqual(200, response.status_code)
    self.assertEqual(insecure_url, form_inputs[fields.LAUNCH_URL])

  def test_get_when_secure_launch_url_set(self):
    secure_url = 'https://something'
    self.tool_config['url'] = secure_url
    self.set_lti_tool_config()
    response = self.testapp.get(lti._LAUNCH_URL, params=self.params)
    form_inputs = self.get_form_inputs(response.body)

    self.assertEqual(200, response.status_code)
    self.assertEqual(secure_url, form_inputs[fields.SECURE_LAUNCH_URL])

  def test_get_when_user_set_renders_signed_form_inputs(self):
    user = users.User(email=self.email)
    self.swap(users, 'get_current_user', lambda: user)
    self.set_lti_tool_config()
    response = self.testapp.get(lti._LAUNCH_URL, params=self.params)
    form_inputs = self.get_form_inputs(response.body)

    self.assertEqual(200, response.status_code)
    self.assert_tool_url_set(response.body)
    self.assert_oauth1_signature_looks_valid(form_inputs)
    self.assert_base_oauth_form_inputs_look_valid(form_inputs)
    self.assert_user_id_equal(self.external_userid, form_inputs)

  def test_get_when_user_unset_renders_signed_form_inputs(self):
    self.set_lti_tool_config()
    response = self.testapp.get(lti._LAUNCH_URL, params=self.params)
    form_inputs = self.get_form_inputs(response.body)

    self.assertEqual(200, response.status_code)
    self.assert_tool_url_set(response.body)
    self.assert_oauth1_signature_looks_valid(form_inputs)
    self.assert_base_oauth_form_inputs_look_valid(form_inputs)
    self.assert_user_not_set(form_inputs)


class LTIToolTagTest(LtiWebappTestBase):

  # TODO(johncox): turn this into an integration test if/when we write a
  # provider. Right now there's nothing to POST to.

  def assert_is_unavailable_schema(self, schema):
    self.assertEqual(1, len(schema._properties))
    self.assertEqual('unused_id', schema._properties[0].name)

  def test_get_schema_returns_populated_schema_when_config_set_and_valid(self):
    self.set_lti_tool_config()
    handler = oeditor.PopupHandler()
    handler.app_context = self.app_context
    tag = lti.LTIToolTag()
    schema = tag.get_schema(handler)

    self.assertEqual('LTI Tool', schema.title)
    self.assertEqual(5, len(schema._properties))

  def test_get_schema_returns_unavailable_schema_when_config_invalid(self):
    self.set_lti_tool_config(config_yaml='-invalid-')
    handler = oeditor.PopupHandler()
    handler.app_context = self.app_context
    tag = lti.LTIToolTag()

    self.assert_is_unavailable_schema(tag.get_schema(handler))

  def test_get_schema_returns_unavailable_schema_when_config_missing(self):
    self.set_lti_tool_config(config_yaml=lti._EMPTY_STRING)
    handler = oeditor.PopupHandler()
    handler.app_context = self.app_context
    tag = lti.LTIToolTag()

    self.assert_is_unavailable_schema(tag.get_schema(handler))


class ParserTestBase(actions.TestBase):

  PARSER = None

  def setUp(self):
    super(ParserTestBase, self).setUp()
    self.errors = []

  def assert_no_errors(self):
    self.assertEqual([], self.errors)

  def assert_parse_error(self):
    self.assertEqual([self.PARSER.PARSE_ERROR], self.errors)


class SecurityParserTest(ParserTestBase):

  PARSER = lti._SecurityParser

  def assert_nonunique_key_error(self, key):
    self.assertEqual([lti._ERROR_KEY_NOT_UNIQUE % key], self.errors)

  def assert_nonunique_secret_error(self, secret):
    self.assertEqual([lti._ERROR_SECRET_NOT_UNIQUE % secret], self.errors)

  def test_empty_string_no_errors_returns_empty_dict(self):
    self.assertEqual({}, self.PARSER.parse(lti._EMPTY_STRING, self.errors))
    self.assert_no_errors()

  def test_one_pair_no_errors(self):
    self.assertEqual(
        {'key1': lti._SecurityConfig('key1', 'value1')},
        self.PARSER.parse('- key1: value1', self.errors))
    self.assert_no_errors()

  def test_multiple_pairs_no_errors(self):
    expected = {
        'key1': lti._SecurityConfig('key1', 'value1'),
        'key2': lti._SecurityConfig('key2', 'value2'),
    }
    self.assertEqual(
      expected,
      self.PARSER.parse('- key1: value1\n- key2: value2', self.errors))
    self.assert_no_errors()

  def test_nonunique_key_error(self):
    self.assertIsNone(
        self.PARSER.parse('- key: secret1\n- key: secret2', self.errors))
    self.assert_nonunique_key_error('key')

  def test_nonunique_secret_error(self):
    self.assertIsNone(
        self.PARSER.parse('- key1: secret\n- key2: secret', self.errors))
    self.assert_nonunique_secret_error('secret')

  def test_parse_error_when_not_list(self):
    self.assertIsNone(self.PARSER.parse('not list', self.errors))
    self.assert_parse_error()

  def test_parse_error_when_list_contains_non_dict(self):
    self.assertIsNone(self.PARSER.parse('- 0', self.errors))
    self.assert_parse_error()

  def test_validate_security_yaml_parse_error_when_safe_load_fails(self):
    self.assertIsNone(self.PARSER.parse(0, self.errors))
    self.assert_parse_error()


class ToolsParserTest(ParserTestBase):

  PARSER = lti._ToolsParser

  def setUp(self):
    super(ToolsParserTest, self).setUp()
    self.values = {
        'description': 'description_value',
        'key': 'key_value',
        'name': 'name_value',
        'secret': 'secret_value',
        'url': 'url_value',
        'version': lti.VERSION_1_2,
    }
    self.second_values = {
        'description': 'second_description',
        'key': 'second_key',
        'name': 'second_name',
        'secret': 'second_secret',
        'url': 'second_url',
        'version': lti.VERSION_1_1,
    }
    self.config = lti._ToolConfig(
        self.values['description'],
        self.values['key'],
        self.values['name'],
        self.values['secret'],
        self.values['url'],
        self.values['version'])
    self.second_config = lti._ToolConfig(
        self.second_values['description'],
        self.second_values['key'],
        self.second_values['name'],
        self.second_values['secret'],
        self.second_values['url'],
        self.second_values['version'])

  def assert_nonunique_name_error(self, name):
    self.assertEqual([lti._ERROR_NAME_NOT_UNIQUE % name], self.errors)

  def test_empty_string_no_errors_returns_empty_dict(self):
    self.assertEqual({}, self.PARSER.parse(lti._EMPTY_STRING, self.errors))

  def test_one_tool_no_errors(self):
    self.assertEquals(
        {self.values['name']: self.config},
        self.PARSER.parse(yaml.safe_dump([self.values]), self.errors))
    self.assert_no_errors()

  def test_multiple_pairs_no_errors(self):
    expected = {
        self.values['name']: self.config,
        self.second_values['name']: self.second_config
    }
    self.assertEqual(
        expected,
        self.PARSER.parse(
            yaml.safe_dump([self.values, self.second_values]), self.errors))
    self.assert_no_errors()

  def test_nonunique_name_error(self):
    self.second_values['name'] = self.values['name']
    self.assertIsNone(
        self.PARSER.parse(
            yaml.safe_dump([self.values, self.second_values]), self.errors))
    self.assert_nonunique_name_error(self.values['name'])

  def test_parse_error_when_not_list(self):
    self.assertIsNone(self.PARSER.parse('not list', self.errors))
    self.assert_parse_error()

  def test_parse_error_when_list_contains_non_dict(self):
    self.assertIsNone(self.PARSER.parse('- 0', self.errors))
    self.assert_parse_error()

  def test_validate_security_yaml_parse_error_when_safe_load_fails(self):
    self.assertIsNone(self.PARSER.parse(0, self.errors))
    self.assert_parse_error()


class SerializerTest(actions.TestBase):

  def test_dump_raises_value_error_if_input_contains_invalid_field(self):
    bad_field = 'bad_field'

    with self.assertRaisesRegexp(ValueError, 'invalid fields: ' + bad_field):
      fields._Serializer.dump('bad_field: value')

  def test_round_trip_of_valid_fields(self):
    valid_field = fields.USER_IMAGE
    value = 'value'
    yaml_string = '%s: %s' % (fields.USER_IMAGE, value)
    self.assertEqual(
        {valid_field: value},
        fields._Serializer.load(fields._Serializer.dump(yaml_string)))
