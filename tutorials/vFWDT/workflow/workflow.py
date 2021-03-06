'''
/*-
* ============LICENSE_START=======================================================
* Copyright (C) 2019 Orange
* ================================================================================
* Licensed under the Apache License, Version 2.0 (the "License");
* you may not use this file except in compliance with the License.
* You may obtain a copy of the License at
*
*      http://www.apache.org/licenses/LICENSE-2.0
*
* Unless required by applicable law or agreed to in writing, software
* distributed under the License is distributed on an "AS IS" BASIS,
* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
* See the License for the specific language governing permissions and
* limitations under the License.
*
* ============LICENSE_END=========================================================
*/
'''

import os
import json
import sys
import uuid
import time
import copy
import warnings
import contextlib
import requests
import simplejson
import http.server
import threading
from datetime import datetime
from datetime import timedelta
from simple_rest_client.api import API
from simple_rest_client.resource import Resource
from basicauth import encode
from urllib3.exceptions import InsecureRequestWarning


old_merge_environment_settings = requests.Session.merge_environment_settings


hostname_cache = []
ansible_inventory = {}
osdf_response = {"last": { "id": "id", "data": None}}
print_performance=False
stats = open("stats.csv", "w")
stats.write("operation;time\n")


class BaseServer(http.server.BaseHTTPRequestHandler):

    def __init__(self, one, two, three):
        self.osdf_resp = osdf_response
        super().__init__(one, two, three)

    def _set_headers(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()

    def do_GET(self):
        self._set_headers()

    def do_HEAD(self):
        self._set_headers()

    def do_POST(self):
        self._set_headers()
        self.data_string = self.rfile.read(int(self.headers['Content-Length']))
        self.send_response(200)
        self.end_headers()

        data = simplejson.loads(self.data_string)
        #print(json.dumps(data, indent=4))
        self.osdf_resp["last"]["data"] = data
        self.osdf_resp["last"]["id"] = data["requestId"]
        with open("response.json", "w") as outfile:
            simplejson.dump(data, outfile)


class timing(object):

    def __init__(self, description):
        self.description = description

    def __call__(self, f):
        def wrap(*args, **kwargs):
            req = None
            if f.__name__ == "appc_lcm_request" or f.__name__ == "confirm_appc_lcm_action":
                req = args[1]
            description = self.description
            if req is not None:
                description = self.description + ' ' + req['input']['action']
            if description.find('>') < 0 and print_performance:
                print (('> {} START').format(description))
            try:
                time1 = time.time()
                ret = f(*args, **kwargs)
            finally:
                time2 = time.time()
                if print_performance:
                    print ('> {} DONE {:0.3f} ms'.format(description, (time2-time1)*1000.0))
                stats.write("{};{:0.3f}\n".format(description, (time2-time1)*1000.0).replace(".", ","))
            return ret
        return wrap


def _run_osdf_resp_server():
    server_address = ('', 9000)
    httpd = http.server.HTTPServer(server_address, BaseServer)
    print('Starting OSDF Response Server...')
    httpd.serve_forever()


@contextlib.contextmanager
def _no_ssl_verification():
    opened_adapters = set()
    def merge_environment_settings(self, url, proxies, stream, verify, cert):
        # Verification happens only once per connection so we need to close
        # all the opened adapters once we're done. Otherwise, the effects of
        # verify=False persist beyond the end of this context manager.
        opened_adapters.add(self.get_adapter(url))

        settings = old_merge_environment_settings(self, url, proxies, stream, verify, cert)
        settings['verify'] = False

        return settings

    requests.Session.merge_environment_settings = merge_environment_settings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', InsecureRequestWarning)
            yield
    finally:
        requests.Session.merge_environment_settings = old_merge_environment_settings

        for adapter in opened_adapters:
            try:
                adapter.close()
            except:
                pass


def _get_aai_rel_link_data(data, related_to, search_key=None, match_dict=None):
    # some strings that we will encounter frequently
    rel_lst = "relationship-list"
    rkey = "relationship-key"
    rval = "relationship-value"
    rdata = "relationship-data"
    response = list()
    if match_dict:
        m_key = match_dict.get('key')
        m_value = match_dict.get('value')
    else:
        m_key = None
        m_value = None
    rel_dict = data.get(rel_lst)
    if rel_dict:  # check if data has relationship lists
        for key, rel_list in rel_dict.items(): # pylint: disable=W0612
            for rel in rel_list:
                if rel.get("related-to") == related_to:
                    dval = None
                    matched = False
                    link = rel.get("related-link")
                    property = ""
                    if rel.get("related-to-property") is not None:
                        property = rel.get("related-to-property")[0]['property-value']
                    r_data = rel.get(rdata, [])
                    if search_key:
                        for rd in r_data:
                            if rd.get(rkey) == search_key:
                                dval = rd.get(rval)
                                if not match_dict:  # return first match
                                    response.append(
                                        {"link": link, "property": property, "d_value": dval}
                                    )
                                    break  # go to next relation
                            if rd.get(rkey) == m_key \
                                    and rd.get(rval) == m_value:
                                matched = True
                        if match_dict and matched:  # if matching required
                            response.append(
                                {"link": link, "property": property, "d_value": dval}
                            )
                            # matched, return search value corresponding
                            # to the matched r_data group
                    else:  # no search key; just return the link
                        response.append(
                            {"link": link, "property": property, "d_value": dval}
                        )
    if response:
        response.append(
            {"link": None, "property": None, "d_value": None}
        )
    return response


class AAIApiResource(Resource):
    actions = {
        'generic_vnf': {'method': 'GET', 'url': 'network/generic-vnfs/generic-vnf/{}'},
        'vf_module': {'method': 'GET', 'url': 'network/generic-vnfs/generic-vnf/{}/vf-modules/vf-module/{}'},
        'vnfc': {'method': 'GET', 'url': 'network/vnfcs/vnfc/{}'},
        'vnfc_put': {'method': 'PUT', 'url': 'network/vnfcs/vnfc/{}'},
        'vnfc_patch': {'method': 'PATCH', 'url': 'network/vnfcs/vnfc/{}'},
        'link': {'method': 'GET', 'url': '{}'},
        'service_instance': {'method': 'GET',
                             'url': 'business/customers/customer/{}/service-subscriptions/service-subscription/{}/service-instances/service-instance/{}'}
    }


class HASApiResource(Resource):
    actions = {
        'plans': {'method': 'POST', 'url': 'plans/'},
        'plan': {'method': 'GET', 'url': 'plans/{}'}
    }


class OSDFApiResource(Resource):
    actions = {
        'placement': {'method': 'POST', 'url': 'placement'}
    }


class APPCLcmApiResource(Resource):
    actions = {
        'distribute_traffic': {'method': 'POST', 'url': 'appc-provider-lcm:distribute-traffic/'},
        'distribute_traffic_check': {'method': 'POST', 'url': 'appc-provider-lcm:distribute-traffic-check/'},
        'upgrade_software': {'method': 'POST', 'url': 'appc-provider-lcm:upgrade-software/'},
        'upgrade_pre_check': {'method': 'POST', 'url': 'appc-provider-lcm:upgrade-pre-check/'},
        'upgrade_post_check': {'method': 'POST', 'url': 'appc-provider-lcm:upgrade-post-check/'},
        'action_status': {'method': 'POST', 'url': 'appc-provider-lcm:action-status/'},
        'check_lock': {'method': 'POST', 'url': 'appc-provider-lcm:check-lock/'},
        'lock': {'method': 'POST', 'url': 'appc-provider-lcm:lock/'},
        'unlock': {'method': 'POST', 'url': 'appc-provider-lcm:unlock/'}
    }


def _init_python_aai_api(onap_ip, content_type='application/json'):
    api = API(
        api_root_url="https://{}:30233/aai/v14/".format(onap_ip),
        params={},
        headers={
            'Authorization': encode("AAI", "AAI"),
            'X-FromAppId': 'SCRIPT',
            'Accept': 'application/json',
            'Content-Type': content_type,
            'X-TransactionId': str(uuid.uuid4()),
        },
        timeout=30,
        append_slash=False,
        json_encode_body=True # encode body as json
    )
    api.add_resource(resource_name='aai', resource_class=AAIApiResource)
    return api


def _init_python_has_api(onap_ip):
    api = API(
        api_root_url="https://{}:30275/v1/".format(onap_ip),
        params={},
        headers={
            'Authorization': encode("admin1", "plan.15"),
            'X-FromAppId': 'SCRIPT',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-TransactionId': str(uuid.uuid4()),
        },
        timeout=30,
        append_slash=False,
        json_encode_body=True # encode body as json
    )
    api.add_resource(resource_name='has', resource_class=HASApiResource)
    return api


def _init_python_osdf_api(onap_ip):
    api = API(
        api_root_url="https://{}:30248/api/oof/v1/".format(onap_ip),
        params={},
        headers={
            'Authorization': encode("test", "testpwd"),
            'X-FromAppId': 'SCRIPT',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-TransactionId': str(uuid.uuid4()),
        },
        timeout=30,
        append_slash=False,
        json_encode_body=True # encode body as json
    )
    api.add_resource(resource_name='osdf', resource_class=OSDFApiResource)
    return api


def _init_python_appc_lcm_api(onap_ip):
    api = API(
        api_root_url="https://{}:30230/restconf/operations/".format(onap_ip),
        params={},
        headers={
            'Authorization': encode("admin", "Kp8bJ4SXszM0WXlhak3eHlcse2gAw84vaoGGmJvUy2U"),
            'X-FromAppId': 'SCRIPT',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        },
        timeout=300,
        append_slash=False,
        json_encode_body=True # encode body as json
    )
    api.add_resource(resource_name='lcm', resource_class=APPCLcmApiResource)
    return api


@timing("Load AAI Data")
def load_aai_data(vfw_vnf_id, onap_ip):
    api = _init_python_aai_api(onap_ip)
    aai_data = {}
    aai_data['service-info'] = {'global-customer-id': '', 'service-instance-id': '', 'service-type': ''}
    aai_data['vfw-model-info'] = {'model-invariant-id': '', 'model-version-id': '', 'vnf-name': '', 'vnf-type': ''}
    aai_data['vpgn-model-info'] = {'model-invariant-id': '', 'model-version-id': '', 'vnf-name': '', 'vnf-type': ''}
    with _no_ssl_verification():
        response = api.aai.generic_vnf(vfw_vnf_id, body=None, params={'depth': 2}, headers={})
        aai_data['vfw-model-info']['model-invariant-id'] = response.body.get('model-invariant-id')
        aai_data['vfw-model-info']['model-version-id'] = response.body.get('model-version-id')
        aai_data['vfw-model-info']['vnf-name'] = response.body.get('vnf-name')
        aai_data['vfw-model-info']['vnf-type'] = response.body.get('vnf-type')
        aai_data['vf-module-id'] = response.body['vf-modules']['vf-module'][0]['vf-module-id']

        related_to = "service-instance"
        search_key = "customer.global-customer-id"
        rl_data_list = _get_aai_rel_link_data(data=response.body, related_to=related_to, search_key=search_key)
        aai_data['service-info']['global-customer-id'] = rl_data_list[0]['d_value']

        search_key = "service-subscription.service-type"
        rl_data_list = _get_aai_rel_link_data(data=response.body, related_to=related_to, search_key=search_key)
        aai_data['service-info']['service-type'] = rl_data_list[0]['d_value']

        search_key = "service-instance.service-instance-id"
        rl_data_list = _get_aai_rel_link_data(data=response.body, related_to=related_to, search_key=search_key)
        aai_data['service-info']['service-instance-id'] = rl_data_list[0]['d_value']

        service_link = rl_data_list[0]['link']
        response = api.aai.link(service_link, body=None, params={}, headers={})

        related_to = "generic-vnf"
        search_key = "generic-vnf.vnf-id"
        rl_data_list = _get_aai_rel_link_data(data=response.body, related_to=related_to, search_key=search_key)
        for i in range(0, len(rl_data_list)):
            vnf_id = rl_data_list[i]['d_value']

            if vnf_id != vfw_vnf_id:
                vnf_link = rl_data_list[i]['link']
                response = api.aai.link(vnf_link, body=None, params={}, headers={})
                if aai_data['vfw-model-info']['model-invariant-id'] != response.body.get('model-invariant-id'):
                    aai_data['vpgn-model-info']['model-invariant-id'] = response.body.get('model-invariant-id')
                    aai_data['vpgn-model-info']['model-version-id'] = response.body.get('model-version-id')
                    aai_data['vpgn-model-info']['vnf-name'] = response.body.get('vnf-name')
                    aai_data['vpgn-model-info']['vnf-type'] = response.body.get('vnf-type')
                    break
    return aai_data


@timing("> OSDF REQ")
def _osdf_request(rancher_ip, onap_ip, aai_data, exclude, use_oof_cache):
    dirname = os.path.join('templates/oof-cache/', aai_data['vf-module-id'])
    if exclude:
        file = os.path.join(dirname, 'sample-osdf-excluded.json')
    else:
        file = os.path.join(dirname, 'sample-osdf-required.json')
    if use_oof_cache and os.path.exists(file):
        migrate_from = json.loads(open(file).read())
        return migrate_from

    print('Making OSDF request for excluded {}'.format(str(exclude)))
    api = _init_python_osdf_api(onap_ip)
    request_id = str(uuid.uuid4())
    transaction_id = str(uuid.uuid4())
    callback_url = "http://{}:9000/osdfCallback/".format(str(rancher_ip))
    template = json.loads(open('templates/osdfRequest.json').read())
    template["requestInfo"]["transactionId"] = transaction_id
    template["requestInfo"]["requestId"] = request_id
    template["requestInfo"]["callbackUrl"] = callback_url
    template["serviceInfo"]["serviceInstanceId"] = aai_data['service-info']['service-instance-id']
    template["placementInfo"]["requestParameters"]["chosenCustomerId"] = aai_data['service-info']['global-customer-id']
    template["placementInfo"]["placementDemands"][0]["resourceModelInfo"]["modelInvariantId"] =\
        aai_data['vfw-model-info']['model-invariant-id']
    template["placementInfo"]["placementDemands"][0]["resourceModelInfo"]["modelVersionId"] =\
        aai_data['vfw-model-info']['model-version-id']
    template["placementInfo"]["placementDemands"][1]["resourceModelInfo"]["modelInvariantId"] =\
        aai_data['vpgn-model-info']['model-invariant-id']
    template["placementInfo"]["placementDemands"][1]["resourceModelInfo"]["modelVersionId"] =\
        aai_data['vpgn-model-info']['model-version-id']
    if exclude:
        template["placementInfo"]["placementDemands"][0]["excludedCandidates"][0]["identifiers"].\
            append(aai_data['vf-module-id'])
        del template["placementInfo"]["placementDemands"][0]["requiredCandidates"]
    else:
        template["placementInfo"]["placementDemands"][0]["requiredCandidates"][0]["identifiers"].\
            append(aai_data['vf-module-id'])
        del template["placementInfo"]["placementDemands"][0]["excludedCandidates"]

    #print(json.dumps(template, indent=4))

    with _no_ssl_verification():
        response = api.osdf.placement(body=template, params={}, headers={}) # pylint: disable=W0612
        #if response.body.get('error_message') is not None:
        #    raise Exception(response.body['error_message']['explanation'])

    counter = 0
    while counter < 600 and osdf_response["last"]["id"] != request_id:
        time.sleep(1)
        if counter % 20 == 0:
            print("solving")
        counter += 1

    if osdf_response["last"]["id"] == request_id:
        status = osdf_response["last"]["data"]["requestStatus"]
        if status == "completed":
            result = {
                "solution": osdf_response["last"]["data"]["solutions"]["placementSolutions"]
            }
            if not os.path.exists(dirname):
                os.makedirs(dirname)
            f = open(file, 'w+')
            f.write(json.dumps(result, indent=4))
            f.close()
            return result
        else:
            message = osdf_response["last"]["data"]["statusMessage"]
            raise Exception("OOF request {}: {}".format(status, message))
    else:
        raise Exception("No response for OOF request")


@timing("> HAS REQ")
def _has_request(onap_ip, aai_data, exclude, use_oof_cache):
    dirname = os.path.join('templates/oof-cache/', aai_data['vf-module-id'])
    if exclude:
        file = os.path.join(dirname, 'sample-has-excluded.json')
    else:
        file = os.path.join(dirname, 'sample-has-required.json')
    if use_oof_cache and os.path.exists(file):
        migrate_from = json.loads(open(file).read())
        return migrate_from

    print('Making HAS request for excluded {}'.format(str(exclude)))
    api = _init_python_has_api(onap_ip)
    request_id = str(uuid.uuid4())
    template = json.loads(open('templates/hasRequest.json').read())
    result = {}
    template['name'] = request_id
    node = template['template']['parameters']
    node['chosen_customer_id'] = aai_data['service-info']['global-customer-id']
    node['service_id'] = aai_data['service-info']['service-instance-id']
    node = template['template']['demands']['vFW-SINK'][0]
    node['filtering_attributes']['model-invariant-id'] = aai_data['vfw-model-info']['model-invariant-id']
    node['filtering_attributes']['model-version-id'] = aai_data['vfw-model-info']['model-version-id']
    if exclude:
        node['excluded_candidates'][0]['candidate_id'][0] = aai_data['vf-module-id']
        del node['required_candidates']
    else:
        node['required_candidates'][0]['candidate_id'][0] = aai_data['vf-module-id']
        del node['excluded_candidates']
    node = template['template']['demands']['vPGN'][0]
    node['filtering_attributes']['model-invariant-id'] = aai_data['vpgn-model-info']['model-invariant-id']
    node['filtering_attributes']['model-version-id'] = aai_data['vpgn-model-info']['model-version-id']

    #print(json.dumps(template, indent=4))

    with _no_ssl_verification():
        response = api.has.plans(body=template, params={}, headers={})
        if response.body.get('error_message') is not None:
            raise Exception(response.body['error_message']['explanation'])
        else:
            plan_id = response.body['id']
            response = api.has.plan(plan_id, body=None, params={}, headers={})
            status = response.body['plans'][0]['status']
            while status != 'done' and status != 'error':
                print(status)
                response = api.has.plan(plan_id, body=None, params={}, headers={})
                status = response.body['plans'][0]['status']
            if status == 'done':
                result = response.body['plans'][0]['recommendations'][0]
            else:
                raise Exception(response.body['plans'][0]['message'])

    if not os.path.exists(dirname):
        os.makedirs(dirname)
    f = open(file, 'w+')
    f.write(json.dumps(result, indent=4))
    f.close()
    return result


def _extract_has_appc_identifiers(has_result, demand, onap_ip):
    if demand == 'vPGN':
        v_server = has_result[demand]['attributes']['vservers'][0]
    else:
        if len(has_result[demand]['attributes']['vservers'][0]['l-interfaces']) == 4:
            v_server = has_result[demand]['attributes']['vservers'][0]
        else:
            v_server = has_result[demand]['attributes']['vservers'][1]
    for itf in v_server['l-interfaces']:
        if itf['ipv4-addresses'][0].startswith("10.0."):
            ip = itf['ipv4-addresses'][0]
            break

    if v_server['vserver-name'] in hostname_cache and demand != 'vPGN':
        v_server['vserver-name'] = v_server['vserver-name'].replace("01", "02")
    hostname_cache.append(v_server['vserver-name'])

    api = _init_python_aai_api(onap_ip) # pylint: disable=W0612
    vnfc_type = demand.lower()
#    with _no_ssl_verification():
#        response = api.aai.vnfc(v_server['vserver-name'], body=None, params={}, headers={})
#        vnfc_type = response.body.get('nfc-naming-code')

    config = {
        'vnf-id': has_result[demand]['attributes']['nf-id'],
        'vf-module-id': has_result[demand]['attributes']['vf-module-id'],
        'ip': ip,
        'vserver-id': v_server['vserver-id'],
        'vserver-name': v_server['vserver-name'],
        'vnfc-type': vnfc_type,
        'physical-location-id': has_result[demand]['attributes']['physical-location-id']
    }
    ansible_inventory_entry = "{} ansible_ssh_host={} ansible_ssh_user=ubuntu".format(config['vserver-name'], config['ip'])
    if demand.lower() not in ansible_inventory:
        ansible_inventory[demand.lower()] = {}
    ansible_inventory[demand.lower()][config['vserver-name']] = ansible_inventory_entry

    _verify_vnfc_data(api, onap_ip, config)
    return config


def _verify_vnfc_data(aai_api, onap_ip, config, root=None):
    vnfc_name = config['vserver-name']
    oam_ip = config['ip']
    with _no_ssl_verification():
        response = aai_api.aai.vnfc(vnfc_name, body=None, params=None, headers={})
    #print(json.dumps(response.body))
    if "ipaddress-v4-oam-vip" not in response.body and oam_ip != "":
        print("VNFC IP information update for {}".format(vnfc_name))
        api = _init_python_aai_api(onap_ip, 'application/merge-patch+json')
        with _no_ssl_verification():
            response = api.aai.vnfc_patch(vnfc_name, body={"ipaddress-v4-oam-vip": oam_ip, "vnfc-name": vnfc_name}, params=None, headers={})
    if "relationship-list" not in response.body:
        print("VNFC REL information update for {}".format(vnfc_name))
        vserver_info = {
            "link": "",
            "owner": "",
            "region": "",
            "tenant": "",
            "id": ""
        }
        with _no_ssl_verification():
            vf_module = aai_api.aai.vf_module(config['vnf-id'], config['vf-module-id'], body=None, params={'depth': 2}, headers={}).body
        related_to = "vserver"
        search_key = "cloud-region.cloud-owner"
        rl_data_list = _get_aai_rel_link_data(data=vf_module, related_to=related_to, search_key=search_key)
        vserver_info["owner"] = rl_data_list[0]['d_value']

        search_key = "cloud-region.cloud-region-id"
        rl_data_list = _get_aai_rel_link_data(data=vf_module, related_to=related_to, search_key=search_key)
        vserver_info["region"] = rl_data_list[0]['d_value']

        search_key = "tenant.tenant-id"
        rl_data_list = _get_aai_rel_link_data(data=vf_module, related_to=related_to, search_key=search_key)
        vserver_info["tenant"] = rl_data_list[0]['d_value']

        search_key = "vserver.vserver-id"
        rl_data_list = _get_aai_rel_link_data(data=vf_module, related_to=related_to, search_key=search_key)
        for relation in rl_data_list:
            vserver_info["id"] = relation['d_value']
            vserver_info["link"] = relation['link']

            rel_data = {
                "related-to": "vserver",
                "related-link": vserver_info["link"],
                "relationship-data": [
                    {
                        "relationship-key": "cloud-region.cloud-owner",
                        "relationship-value": vserver_info["owner"]
                    },
                    {
                        "relationship-key": "cloud-region.cloud-region-id",
                        "relationship-value": vserver_info["region"]
                    },
                    {
                        "relationship-key": "tenant.tenant-id",
                        "relationship-value": vserver_info["tenant"]
                    },
                    {
                        "relationship-key": "vserver.vserver-id",
                        "relationship-value": vserver_info["id"]
                    }
                ]
            }
            #print(json.dumps(rel_data, indent=4))
            if config['vserver-id'] == relation['d_value']:
                with _no_ssl_verification():
                    response = aai_api.aai.vnfc_put("{}/relationship-list/relationship".format(vnfc_name), body=rel_data, params=None, headers={})
            elif root is None and relation['d_value'] is not None:
                new_config = copy.deepcopy(config)
                new_config['vserver-name'] = relation['property']
                new_config['vserver-id'] = relation['d_value']
                new_config['ip'] = ""
                _verify_vnfc_data(aai_api, onap_ip, new_config, vnfc_name)
    with _no_ssl_verification():
        response = aai_api.aai.vnfc(vnfc_name, body=None, params=None, headers={})
        #print(json.dumps(response.body))


def _extract_osdf_appc_identifiers(has_result, demand, onap_ip):
    if demand == 'vPGN':
        v_server = has_result[demand]['vservers'][0]
    else:
        if len(has_result[demand]['vservers'][0]['l-interfaces']) == 4:
            v_server = has_result[demand]['vservers'][0]
        else:
            v_server = has_result[demand]['vservers'][1]
    for itf in v_server['l-interfaces']:
        if itf['ipv4-addresses'][0].startswith("10.0."):
            ip = itf['ipv4-addresses'][0]
            break

    if v_server['vserver-name'] in hostname_cache and demand != 'vPGN':
        v_server['vserver-name'] = v_server['vserver-name'].replace("01", "02")
    hostname_cache.append(v_server['vserver-name'])

    api = _init_python_aai_api(onap_ip)
    vnfc_type = demand.lower(),
    with _no_ssl_verification():
        response = api.aai.vnfc(v_server['vserver-name'], body=None, params={}, headers={})
        vnfc_type = response.body.get('nfc-naming-code')

    config = {
        'vnf-id': has_result[demand]['nf-id'],
        'vf-module-id': has_result[demand]['vf-module-id'],
        'ip': ip,
        'vserver-id': v_server['vserver-id'],
        'vserver-name': v_server['vserver-name'],
        'vnfc-type': vnfc_type,
        'physical-location-id': has_result[demand]['locationId']
    }
    ansible_inventory_entry = "{} ansible_ssh_host={} ansible_ssh_user=ubuntu".format(config['vserver-name'], config['ip'])
    if demand.lower() not in ansible_inventory:
        ansible_inventory[demand.lower()] = {}
    ansible_inventory[demand.lower()][config['vserver-name']] = ansible_inventory_entry

    _verify_vnfc_data(api, onap_ip, config)

    return config


def _extract_has_appc_dt_config(has_result, demand):
    if demand == 'vPGN':
        return {}
    else:
        config = {
            "nf-type": has_result[demand]['attributes']['nf-type'],
            "nf-name": has_result[demand]['attributes']['nf-name'],
            "vf-module-name": has_result[demand]['attributes']['vf-module-name'],
            "vnf-type": has_result[demand]['attributes']['vnf-type'],
            "service_instance_id": "319e60ef-08b1-47aa-ae92-51b97f05e1bc",
            "cloudClli": has_result[demand]['attributes']['physical-location-id'],
            "nf-id": has_result[demand]['attributes']['nf-id'],
            "vf-module-id": has_result[demand]['attributes']['vf-module-id'],
            "aic_version": has_result[demand]['attributes']['aic_version'],
            "ipv4-oam-address": has_result[demand]['attributes']['ipv4-oam-address'],
            "vnfHostName": has_result[demand]['candidate']['host_id'],
            "cloudOwner": has_result[demand]['candidate']['cloud_owner'],
            "isRehome": has_result[demand]['candidate']['is_rehome'],
            "locationId": has_result[demand]['candidate']['location_id'],
            "locationType": has_result[demand]['candidate']['location_type'],
            'vservers': has_result[demand]['attributes']['vservers']
        }
        return config


def _extract_osdf_appc_dt_config(osdf_result, demand):
    if demand == 'vPGN':
        return {}
    else:
        return osdf_result[demand]


def _build_config_from_has(has_result, onap_ip):
    v_pgn_result = _extract_has_appc_identifiers(has_result, 'vPGN', onap_ip)
    v_fw_result = _extract_has_appc_identifiers(has_result, 'vFW-SINK', onap_ip)
    dt_config = _extract_has_appc_dt_config(has_result, 'vFW-SINK')

    config = {
        'vPGN': v_pgn_result,
        'vFW-SINK': v_fw_result
    }
    #print(json.dumps(config, indent=4))
    config['dt-config'] = {
        'destinations': [dt_config]
    }
    return config


def _adapt_osdf_result(osdf_result):
    result = {}
    demand = _build_osdf_result_demand(osdf_result["solution"][0][0])
    result[demand["name"]] = demand["value"]
    demand = _build_osdf_result_demand(osdf_result["solution"][0][1])
    result[demand["name"]] = demand["value"]
    return result


def _build_osdf_result_demand(solution):
    result = {}
    result["name"] = solution["resourceModuleName"]
    value = {"candidateId": solution["solution"]["identifiers"][0]}
    for info in solution["assignmentInfo"]:
        value[info["key"]] = info["value"]
    result["value"] = value
    return result


def _build_config_from_osdf(osdf_result, onap_ip):
    osdf_result = _adapt_osdf_result(osdf_result)
    v_pgn_result = _extract_osdf_appc_identifiers(osdf_result, 'vPGN', onap_ip)
    v_fw_result = _extract_osdf_appc_identifiers(osdf_result, 'vFW-SINK', onap_ip)
    dt_config = _extract_osdf_appc_dt_config(osdf_result, 'vFW-SINK')

    config = {
        'vPGN': v_pgn_result,
        'vFW-SINK': v_fw_result
    }
    #print(json.dumps(config, indent=4))
    config['dt-config'] = {
        'destinations': [dt_config]
    }
    return config


def _build_appc_lcm_dt_payload(demand, oof_config, action, traffic_presence):
    is_check = traffic_presence is not None
    oof_config = copy.deepcopy(oof_config)
    #if is_vpkg:
    #    node_list = "[ {} ]".format(oof_config['vPGN']['vserver-id'])
    #else:
    #    node_list = "[ {} ]".format(oof_config['vFW-SINK']['vserver-id'])
    book_name = "{}/latest/ansible/{}/site.yml".format(demand.lower(), action.lower())
    config = oof_config[demand]
    #node = {
    #    'site': config['physical-location-id'],
    #    'vnfc_type': config['vnfc-type'],
    #    'vm_info': [{
    #        'ne_id': config['vserver-name'],
    #        'fixed_ip_address': config['ip']
    #   }]
    #}
    #node_list = list()
    #node_list.append(node)

    if is_check:
        oof_config['dt-config']['trafficpresence'] = traffic_presence

    file_content = oof_config['dt-config']

    config = {
        "configuration-parameters": {
            "file_parameter_content":  json.dumps(file_content)
        },
        "request-parameters": {
            "vserver-id": config['vserver-id']
        }
    }
    if book_name != '':
        config["configuration-parameters"]["book_name"] = book_name
    payload = json.dumps(config)
    return payload


def _build_appc_lcm_upgrade_payload(demand, oof_config, action, old_version, new_version):
    oof_config = copy.deepcopy(oof_config)
    book_name = "{}/latest/ansible/{}/site.yml".format(demand.lower(), action.lower())
    config = oof_config[demand]

    file_content = {}  #oof_config['dt-config']

    config = {
        "configuration-parameters": {
            "file_parameter_content":  json.dumps(file_content),
            "existing-software-version": old_version,
            "new-software-version": new_version
        },
        "request-parameters": {
            "vserver-id": config['vserver-id']
        }
    }
    if book_name != '':
        config["configuration-parameters"]["book_name"] = book_name
    payload = json.dumps(config)
    return payload


def _build_appc_lcm_status_body(req):
    payload = {
        'request-id': req['input']['common-header']['request-id'],
        'sub-request-id': req['input']['common-header']['sub-request-id'],
        'originator-id': req['input']['common-header']['originator-id']
    }
    payload = json.dumps(payload)
    template = json.loads(open('templates/appcRestconfLcm.json').read())
    template['input']['action'] = 'ActionStatus'
    template['input']['payload'] = payload
    template['input']['common-header']['request-id'] = req['input']['common-header']['request-id']
    template['input']['common-header']['sub-request-id'] = str(uuid.uuid4())
    template['input']['action-identifiers']['vnf-id'] = req['input']['action-identifiers']['vnf-id']
    return template


@timing("> DT REQ BODY")
def _build_appc_lcm_lock_request_body(is_vpkg, config, req_id, action):
    if is_vpkg:
        demand = 'vPGN'
    else:
        demand = 'vFW-SINK'
    return _build_appc_lcm_request_body(None, demand, config, req_id, action)


@timing("> DT REQ BODY")
def _build_appc_lcm_dt_request_body(is_vpkg, config, req_id, action, traffic_presence=None):
    if is_vpkg:
        demand = 'vPGN'
    else:
        demand = 'vFW-SINK'
    payload = _build_appc_lcm_dt_payload(demand, config, action, traffic_presence)
    return _build_appc_lcm_request_body(payload, demand, config, req_id, action)


@timing("> UP REQ BODY")
def _build_appc_lcm_upgrade_request_body(config, req_id, action, old_version, new_version):
    demand = 'vFW-SINK'
    payload = _build_appc_lcm_upgrade_payload(demand, config, action, old_version, new_version)
    return _build_appc_lcm_request_body(payload, demand, config, req_id, action)


def _build_appc_lcm_request_body(payload, demand, config, req_id, action):
    #print(config[demand])
    template = json.loads(open('templates/appcRestconfLcm.json').read())
    template['input']['action'] = action
    if payload is not None:
        template['input']['payload'] = payload
    else:
        del template['input']['payload']
    template['input']['common-header']['request-id'] = req_id
    template['input']['common-header']['sub-request-id'] = str(uuid.uuid4())
    template['input']['action-identifiers']['vnf-id'] = config[demand]['vnf-id']
    return template


def _set_appc_lcm_timestamp(body, timestamp=None):
    if timestamp is None:
        t = datetime.utcnow() + timedelta(seconds=-10)
        timestamp = t.strftime('%Y-%m-%dT%H:%M:%S.244Z')
    body['input']['common-header']['timestamp'] = timestamp


@timing("Load OOF Data and Build APPC REQ")
def build_appc_lcms_requests_body(rancher_ip, onap_ip, aai_data, use_oof_cache, if_close_loop_vfw, new_version=None):
    if_has = False

    if if_has:
        migrate_from = _has_request(onap_ip, aai_data, False, use_oof_cache)

        if if_close_loop_vfw:
            migrate_to = migrate_from
        else:
            migrate_to = _has_request(onap_ip, aai_data, True, use_oof_cache)

        migrate_from = _build_config_from_has(migrate_from, onap_ip)
        migrate_to = _build_config_from_has(migrate_to, onap_ip)
    else:
        migrate_from = _osdf_request(rancher_ip, onap_ip, aai_data, False, use_oof_cache)

        if if_close_loop_vfw:
            migrate_to = migrate_from
        else:
            migrate_to = _osdf_request(rancher_ip, onap_ip, aai_data, True, use_oof_cache)

        migrate_from = _build_config_from_osdf(migrate_from, onap_ip)
        migrate_to = _build_config_from_osdf(migrate_to, onap_ip)

    #print(json.dumps(migrate_from, indent=4))
    #print(json.dumps(migrate_to, indent=4))
    req_id = str(uuid.uuid4())
    result = list()
    old_version = "2.0"
    if_dt_only = new_version is None
    if new_version is not None and new_version != "1.0":
        old_version = "1.0"

    requests = list()
    include_lock = True

    if include_lock:
        result.append({"payload": _build_appc_lcm_lock_request_body(True, migrate_from, req_id, 'CheckLock'), "breakOnFailure": True,
                      "description": "Check vPGN Lock Status"})
        result.append({"payload": _build_appc_lcm_lock_request_body(False, migrate_from, req_id, 'CheckLock'), "breakOnFailure": True,
                      "description": "Check vFW-1 Lock Status"})
        result.append({"payload": _build_appc_lcm_lock_request_body(False, migrate_to, req_id, 'CheckLock'), "breakOnFailure": True,
                      "description": "Check vFW-2 Lock "})

        result.append({"payload": _build_appc_lcm_lock_request_body(True, migrate_from, req_id, 'Lock'), "breakOnFailure": True,
                      "description": "Lock vPGN"})
        result.append({"payload": _build_appc_lcm_lock_request_body(False, migrate_from, req_id, 'Lock'), "breakOnFailure": True,
                      "description": "Lock vFW-1"})
        result.append({"payload": _build_appc_lcm_lock_request_body(False, migrate_to, req_id, 'Lock'), "breakOnFailure": True,
                      "description": "Lock vFW-2"})

    if if_dt_only:
        payload_dt_check_vpkg = _build_appc_lcm_dt_request_body(True, migrate_from, req_id, 'DistributeTrafficCheck', True)
        payload_dt_vpkg_to = _build_appc_lcm_dt_request_body(True, migrate_to, req_id, 'DistributeTraffic')
        payload_dt_check_vfw_from = _build_appc_lcm_dt_request_body(False, migrate_from, req_id, 'DistributeTrafficCheck',
                                                                    False)
        payload_dt_check_vfw_to = _build_appc_lcm_dt_request_body(False, migrate_to, req_id, 'DistributeTrafficCheck', True)

        requests.append({"payload": payload_dt_vpkg_to, "breakOnFailure": True, "description": "Migrating source vFW traffic to destination vFW"})
        requests.append({"payload": payload_dt_check_vfw_from, "breakOnFailure": True, "description": "Checking traffic has been stopped on the source vFW"})
        requests.append({"payload": payload_dt_check_vfw_to, "breakOnFailure": True, "description": "Checking traffic has appeared on the destination vFW"})
        result.append({"payload": payload_dt_check_vpkg, "breakOnFailure": False, "description": "Check current traffic destination on vPGN",
                      "workflow": {"requests": requests, "description": "Migrate Traffic and Verify"}})
    else:
        #_build_appc_lcm_dt_request_body(is_vpkg, config, req_id, action, traffic_presence=None):
        payload_dt_check_vpkg = _build_appc_lcm_dt_request_body(True, migrate_from, req_id, 'DistributeTrafficCheck', True)
        payload_dt_vpkg_to = _build_appc_lcm_dt_request_body(True, migrate_to, req_id, 'DistributeTraffic')
        payload_dt_vpkg_from = _build_appc_lcm_dt_request_body(True, migrate_from, req_id, 'DistributeTraffic')

        payload_dt_check_vfw_from_absent = _build_appc_lcm_dt_request_body(False, migrate_from, req_id, 'DistributeTrafficCheck', False)
        payload_dt_check_vfw_to_present = _build_appc_lcm_dt_request_body(False, migrate_to, req_id, 'DistributeTrafficCheck', True)
        payload_dt_check_vfw_to_absent = _build_appc_lcm_dt_request_body(False, migrate_to, req_id, 'DistributeTrafficCheck', False)
        payload_dt_check_vfw_from_present = _build_appc_lcm_dt_request_body(False, migrate_from, req_id, 'DistributeTrafficCheck', True)

        payload_old_version_check_vfw_from =  _build_appc_lcm_upgrade_request_body(migrate_from, req_id, 'UpgradePreCheck', old_version, new_version)
        payload_new_version_check_vfw_from =  _build_appc_lcm_upgrade_request_body(migrate_from, req_id, 'UpgradePostCheck', old_version, new_version)
        payload_upgrade_vfw_from =  _build_appc_lcm_upgrade_request_body(migrate_from, req_id, 'UpgradeSoftware', old_version, new_version)

        migrate_requests = list()
        migrate_requests.append({"payload": payload_dt_vpkg_to, "breakOnFailure": True, "description": "Migrating source vFW traffic to destination vFW"})
        migrate_requests.append({"payload": payload_dt_check_vfw_from_absent, "breakOnFailure": True, "description": "Checking traffic has been stopped on the source vFW"})
        migrate_requests.append({"payload": payload_dt_check_vfw_to_present, "breakOnFailure": True, "description": "Checking traffic has appeared on the destination vFW"})

        requests.append({"payload": payload_dt_check_vpkg, "breakOnFailure": False, "description": "Check current traffic destination on vPGN",
                        "workflow": {"requests": migrate_requests, "description": "Migrate Traffic and Verify"}})
        requests.append({"payload": payload_upgrade_vfw_from, "breakOnFailure": True, "description": "Upgrading Software on source vFW"})
        requests.append({"payload": payload_new_version_check_vfw_from, "breakOnFailure": True, "description": "Check current software version on source vFW"})
        requests.append({"payload": payload_dt_vpkg_from, "breakOnFailure": True, "description": "Migrating destination vFW traffic to source vFW"})
        requests.append({"payload": payload_dt_check_vfw_to_absent, "breakOnFailure": True, "description": "Checking traffic has been stopped on the destination vFW"})
        requests.append({"payload": payload_dt_check_vfw_from_present, "breakOnFailure": True, "description": "Checking traffic has appeared on the source vFW"})

        result.append({"payload": payload_old_version_check_vfw_from, "breakOnFailure": False, "description": "Check current software version on source vFW",
                      "workflow": {"requests": requests, "description": "Migrate Traffic and Upgrade Software"}})

    if include_lock:
        result.append({"payload": _build_appc_lcm_lock_request_body(True, migrate_from, req_id, 'Unlock'), "breakOnFailure": False,
                      "description": "Unlock vPGN"})
        result.append({"payload": _build_appc_lcm_lock_request_body(False, migrate_from, req_id, 'Unlock'), "breakOnFailure": False,
                      "description": "Unlock vFW-1"})
        result.append({"payload": _build_appc_lcm_lock_request_body(False, migrate_to, req_id, 'Unlock'), "breakOnFailure": False,
                      "description": "Unlock vFW-2"})

    return result


@timing("> Execute APPC REQ")
def appc_lcm_request(onap_ip, req):
    #print(req)
    api = _init_python_appc_lcm_api(onap_ip)
    with _no_ssl_verification():
    #print(json.dumps(req, indent=4))
        if req['input']['action'] == "DistributeTraffic":
            result = api.lcm.distribute_traffic(body=req, params={}, headers={})
        elif req['input']['action'] == "DistributeTrafficCheck":
            result = api.lcm.distribute_traffic_check(body=req, params={}, headers={})
        elif req['input']['action'] == "UpgradeSoftware":
            result = api.lcm.upgrade_software(body=req, params={}, headers={})
        elif req['input']['action'] == "UpgradePreCheck":
            result = api.lcm.upgrade_pre_check(body=req, params={}, headers={})
        elif req['input']['action'] == "UpgradePostCheck":
            result = api.lcm.upgrade_post_check(body=req, params={}, headers={})
        elif req['input']['action'] == "CheckLock":
            result = api.lcm.check_lock(body=req, params={}, headers={})
        elif req['input']['action'] == "Lock":
            result = api.lcm.lock(body=req, params={}, headers={})
        elif req['input']['action'] == "Unlock":
            result = api.lcm.unlock(body=req, params={}, headers={})
        else:
            raise Exception("{} action not supported".format(req['input']['action']))

    if result.body['output']['status']['code'] == 400:
        if req['input']['action'] == "CheckLock":
            if result.body['output']['locked'] == "FALSE":
                print("UNLOCKED")
            else:
                print("LOCKED")
                result.body['output']['status']['code'] = 401
        else:
            print("SUCCESSFUL")
    elif result.body['output']['status']['code'] == 100:
        print("ACCEPTED")
    elif result.body['output']['status']['code'] >= 300 and result.body['output']['status']['code'] < 400:
        print("APPC LCM <<{}>> REJECTED [{} - {}]".format(req['input']['action'], result.body['output']['status']['code'],
                                         result.body['output']['status']['message']))
    elif result.body['output']['status']['code'] > 400 and result.body['output']['status']['code'] < 500:
        print("APPC LCM <<{}>> FAILED [{} - {}]".format(req['input']['action'], result.body['output']['status']['code'],
                                         result.body['output']['status']['message']))
#    elif result.body['output']['status']['code'] == 311:
#        timestamp = result.body['output']['common-header']['timestamp']
#        _set_appc_lcm_timestamp(req, timestamp)
#        appc_lcm_request(onap_ip, req)
#        return
    else:
        raise Exception("{} - {}".format(result.body['output']['status']['code'],
                                         result.body['output']['status']['message']))
    #print(result)
    return result.body['output']['status']['code']


def appc_lcm_status_request(onap_ip, req):
    api = _init_python_appc_lcm_api(onap_ip)
    status_body = _build_appc_lcm_status_body(req)
    _set_appc_lcm_timestamp(status_body)
    #print("CHECK STATUS")
    with _no_ssl_verification():
        result = api.lcm.action_status(body=status_body, params={}, headers={})

    if result.body['output']['status']['code'] == 400:
        status = json.loads(result.body['output']['payload'])
        return status
    else:
        raise Exception("{} - {}".format(result.body['output']['status']['code'],
                                         result.body['output']['status']['message']))


@timing("> Confirm APPC REQ")
def confirm_appc_lcm_action(onap_ip, req, check_appc_result):
    print("APPC LCM << {} >> [Status]".format(req['input']['action']))

    while True:
        time.sleep(2)
        status = appc_lcm_status_request(onap_ip, req)
        print(status['status'])
        if status['status'] == 'SUCCESSFUL':
            return True
        elif status['status'] == 'IN_PROGRESS':
            continue
        elif check_appc_result:
            print("APPC LCM <<{}>> [{} - {}]".format(req['input']['action'], status['status'], status['status-reason']))
            return False
        else:
            return True


@timing("Execute APPC LCM REQs")
def _execute_lcm_requests(workflow, onap_ip, check_result):
    lcm_requests = workflow["requests"]
    print("WORKFLOW << {} >>".format(workflow["description"]))
    for i in range(len(lcm_requests)):
        req = lcm_requests[i]["payload"]
        #print(json.dumps(req, indent=4))
        print("APPC LCM << {} >> [{}]".format(req['input']['action'], lcm_requests[i]["description"]))
        _set_appc_lcm_timestamp(req)
        conf_result = False
        result = appc_lcm_request(onap_ip, req)
        #print("Result {}".format(result))

        if result == 100:
            conf_result = confirm_appc_lcm_action(onap_ip, req, check_result)
            #time.sleep(30)
        elif result == 400:
            conf_result = True

        if not conf_result:
            if lcm_requests[i]["breakOnFailure"]:
                raise Exception("APPC LCM << {} >> FAILED".format(req['input']['action']))
            elif "workflow" in lcm_requests[i]:
                print("WORKFLOW << {} >> SKIP".format(lcm_requests[i]["workflow"]["description"]))
        elif "workflow" in lcm_requests[i]:
            _execute_lcm_requests(lcm_requests[i]["workflow"], onap_ip, check_result)


def _generate_cdt_artifact_request(req_id, artifact, action, vnfc_type):
    req = {
      'input': {
          'design-request': {
              'request-id': req_id,
              'action': "uploadArtifact",
              'payload': json.dumps(artifact['payload'])
          }
       }
    }

    file = "{}_{}_{}.json".format(artifact['type'], action.lower(), vnfc_type)
    dirname = "templates/cdt-requests"
    #print(file)
    if not os.path.exists(dirname):
        os.makedirs(dirname)
    f = open("{}/{}".format(dirname, file), 'w')
    f.write(json.dumps(req, indent=4))
    f.close()

    return req


def _get_name_of_artifact(prefix, action, vnf_type):
    return "{}_{}_{}_0.0.1V.json".format(prefix, action, vnf_type)


def _set_artifact_payload(vnf_type, vnfc_type, action, artifact):
    sw_upgrade = False
    if action == "DistributeTraffic" or action == "DistributeTrafficCheck" or action == "AllAction":
        pass
    elif action == "UpgradeSoftware" or action == "UpgradePreCheck" or action == "UpgradePostCheck":
        sw_upgrade = True
    else:
        raise Exception("{} action not supported".format(action))

    artifact_contents = ''
    if artifact['type'] == 'config_template':
        file = 'templates/cdt-templates/templates/action-template.json'
        template_file = 'templates/cdt-templates/{}/{}'
        if sw_upgrade:
            template_file = template_file.format(vnfc_type, 'upgrade.json')
        else:
            template_file = template_file.format(vnfc_type, 'traffic.json')
        #print("Template for action {} in {}".format(action, template_file))
        #print(json.dumps(json.loads(open(template_file).read()), indent=4))
        artifact_contents = json.dumps(json.loads(open(template_file).read()), indent=4).replace("\n", "\r\n")
    elif artifact['type'] == 'parameter_definitions':
        file = 'templates/cdt-templates/templates/{}'
        if sw_upgrade:
            file = file.format('upgrade-params.json')
        else:
            file = file.format('traffic-params.json')
    elif artifact['type'] == 'param_values':
        file = 'templates/cdt-templates/templates/{}'
        if sw_upgrade:
            file = file.format('upgrade-params-list.json')
        else:
            file = file.format('traffic-params-list.json')
    elif artifact['type'] == 'reference_template':
        file = 'templates/cdt-templates/templates/reference-all-actions.json'
    else:
        raise Exception("{} not supported artifact type".format(artifact['type']))

    payload = json.loads(open(file).read())
    payload['vnf-type'] = vnf_type
    payload['artifact-name'] = artifact['name']
    payload['action'] = action

    if artifact['type'] == 'config_template':
        payload['artifact-contents'] = artifact_contents
    artifact['payload'] = payload


def _generate_artifacts_for_cdt(vnf_type, vnf_type_formatted, vnfc_type, action):
    artifacts = []
    artifacts.append({
        'name': _get_name_of_artifact("template", action, vnf_type_formatted),
        'type': 'config_template',
        'payload': {'test': 'test'}
    })
    artifacts.append({
        'name': _get_name_of_artifact("pd", action, vnf_type_formatted),
        'type': 'parameter_definitions',
        'payload': {'test': 'test'}
    })
    artifacts.append({
        'name': _get_name_of_artifact("param", action, vnf_type_formatted),
        'type': 'param_values',
        'payload': {'test': 'test'}
    })

    _set_artifact_payload(vnf_type, vnfc_type, action, artifacts[0])
    _set_artifact_payload(vnf_type, vnfc_type, action, artifacts[1])
    _set_artifact_payload(vnf_type, vnfc_type, action, artifacts[2])

    return artifacts


def _generate_cdt_payloads_for_vnf(vnf_info, vnfc_type, actions):
    req_id = str(uuid.uuid4()).replace('-','')
    vnf_type_formatted = vnf_info['vnf-type'].replace(' ','').replace('/', '_')
    artifacts = {
        'AllAction': [{
            'name': _get_name_of_artifact("reference", 'AllAction', vnf_type_formatted),
            'type': 'reference_template'
        }]
    }

    all_action_artifact = artifacts['AllAction'][0]

    _set_artifact_payload(vnf_info['vnf-type'], vnfc_type, 'AllAction', all_action_artifact)

    for action in actions:
        action_artifacts = _generate_artifacts_for_cdt(vnf_info['vnf-type'], vnf_type_formatted, vnfc_type, action)
        artifacts[action] = action_artifacts

    all_action_artifacts = list()

    for action in artifacts:
        artifact_list = list()
        action_info = {
            'action': action,
            'action-level': "vnf",
            'scope': {
                 'vnf-type': vnf_info['vnf-type'],
                 'vnfc-type-list': [],
                 'vnfc-type': ""
            },
            'artifact-list': artifact_list
        }

        if action != 'AllAction':
            action_info.update({
                'template': "Y",
                'vm': [],
                'device-protocol': "ANSIBLE",
                'user-name': "admin",
                'port-number': "8000",
                'scopeType': "vnf-type"
            })

        for action_artifact in artifacts[action]:
            artifact_list.append({'artifact-name': action_artifact['name'], 'artifact-type': action_artifact['type']})
            if action != 'AllAction':
                req = _generate_cdt_artifact_request(req_id, action_artifact, action, vnfc_type) # pylint: disable=W0612
                #print(json.dumps(req, indent=4))

        #print(json.dumps(action_info, indent=4))
        all_action_artifacts.append(action_info)

    all_action_artifact['payload']['artifact-contents'] = json.dumps({'reference_data': all_action_artifacts})
    req = _generate_cdt_artifact_request(req_id, all_action_artifact, 'AllAction', vnfc_type)
    #print(json.dumps(req, indent=4))


def _generate_cdt_payloads(aai_data):
    vfw_actions = ["DistributeTrafficCheck", "UpgradeSoftware", "UpgradePreCheck", "UpgradePostCheck", "UpgradeSoftware"]
    vpgn_actions = ["DistributeTraffic", "DistributeTrafficCheck"]
    _generate_cdt_payloads_for_vnf(aai_data["vfw-model-info"], "vfw-sink", vfw_actions)
    _generate_cdt_payloads_for_vnf(aai_data["vpgn-model-info"], "vpgn", vpgn_actions)


def execute_workflow(vfw_vnf_id, rancher_ip, onap_ip, use_oof_cache, if_close_loop_vfw, info_only, check_result, new_version=None):
    print("\nExecuting workflow for VNF ID '{}' on Rancher with IP {} and ONAP with IP {}".format(
        vfw_vnf_id, rancher_ip, onap_ip))
    print("\nOOF Cache {}, is CL vFW {}, only info {}, check LCM result {}".format(use_oof_cache, if_close_loop_vfw,
                                                                                   info_only, check_result))
    if new_version is not None:
        print("\nNew vFW software version {}\n".format(new_version))

    x = threading.Thread(target=_run_osdf_resp_server, daemon=True)
    x.start()
    aai_data = load_aai_data(vfw_vnf_id, onap_ip)
    print("\nvFWDT Service Information:")
    print(json.dumps(aai_data, indent=4))
    lcm_requests = build_appc_lcms_requests_body(rancher_ip, onap_ip, aai_data, use_oof_cache, if_close_loop_vfw, new_version)
    print("\nAnsible Inventory:")
    inventory = "[host]\nlocalhost   ansible_connection=local\n"
    for key in ansible_inventory:
        inventory += str("[{}]\n").format(key)
        for host in ansible_inventory[key]:
            inventory += str("{}\n").format(ansible_inventory[key][host])

    print(inventory)
    f = open("Ansible_inventory", 'w+')
    f.write(inventory)
    f.close()

    _generate_cdt_payloads(aai_data)

    if info_only:
        return
    print("\nDistribute Traffic Workflow Execution:")

    _execute_lcm_requests({"requests": lcm_requests, "description": "Migrate vFW Traffic Conditionally"}, onap_ip, check_result)



help = """\npython3 workflow.py <VNF-ID> <RANCHER-NODE-IP> <K8S-NODE-IP> <IF-CACHE> <IF-VFWCL> <INITIAL-ONLY> <CHECK-STATUS> <VERSION>
\n<VNF-ID> - vnf-id of vFW VNF instance that traffic should be migrated out from
<RANCHER-NODE-IP> - External IP of ONAP Rancher Node i.e. 10.12.5.160 (If Rancher Node is missing this is NFS node)
<K8S-NODE-IP> - External IP of ONAP K8s Worker Node i.e. 10.12.5.212
<IF-CACHE> - If script should use and build OOF response cache (cache it speed-ups further executions of script)
<IF-VFWCL> - If instead of vFWDT service instance vFW or vFWCL one is used (should be False always)
<INITIAL-ONLY> - If only configuration information will be collected (True for initial phase and False for full execution of workflow)
<CHECK-STATUS> - If APPC LCM action status should be verified and FAILURE should stop workflow (when False FAILED status of LCM action does not stop execution of further LCM actions)
<VERSION> - New version of vFW - for tests '1.0' or '2.0'. Ommit when traffic distribution only\n"""

for key in sys.argv:
    if key == "-h" or key == "--help":
        print(help)
        sys.exit()

new_version = None
if len(sys.argv) > 8:
    new_version = sys.argv[8]

try:
    #vnf_id, Rancher node IP, K8s node IP, use OOF cache, if close loop vfw, if info_only, if check APPC result
    execute_workflow(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4].lower() == 'true', sys.argv[5].lower() == 'true',
                     sys.argv[6].lower() == 'true', sys.argv[7].lower() == 'true', new_version)
finally:
    stats.close()
