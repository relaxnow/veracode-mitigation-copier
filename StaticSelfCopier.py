import argparse
import csv
import logging
import datetime
import logging
import time
import requests
import http.client as http_client

import anticrlf
from veracode_api_py.api import VeracodeAPI as vapi, Applications, Findings, Sandboxes
from veracode_api_py.constants import Constants

log = logging.getLogger(__name__)

def setup_logger(debug):
    handler = logging.FileHandler('MitigationCopier.log', encoding='utf8')
    handler.setFormatter(anticrlf.LogFormatter('%(asctime)s - %(levelname)s - %(funcName)s - %(message)s'))
    log = logging.getLogger(__name__)
    log.addHandler(handler)
    if debug:
        http_client.HTTPConnection.debuglevel = 1
        log.setLevel(logging.DEBUG)
        requests_log = logging.getLogger("requests.packages.urllib3")
        requests_log.setLevel(logging.DEBUG)
        requests_log.propagate = True
    else:
        log.setLevel(logging.INFO)


def creds_expire_days_warning():
    creds = vapi().get_creds()
    exp = datetime.datetime.strptime(creds['expiration_ts'], "%Y-%m-%dT%H:%M:%S.%f%z")
    delta = exp - datetime.datetime.now().astimezone() #we get a datetime with timezone...
    if (delta.days < 7):
        print('These API credentials expire ', creds['expiration_ts'])

def prompt_for_app(prompt_text):
    appguid = ""
    app_name_search = input(prompt_text)
    app_candidates = Applications().get_by_name(app_name_search)
    if len(app_candidates) == 0:
        print("No matches were found!")
    elif len(app_candidates) > 1:
        print("Please choose an application:")
        for idx, appitem in enumerate(app_candidates,start=1):
            print("{}) {}".format(idx, appitem["profile"]["name"]))
        i = input("Enter number: ")
        try:
            if 0 < int(i) <= len(app_candidates):
                appguid = app_candidates[int(i)-1].get('guid')
        except ValueError:
            appguid = ""
    else:
        appguid = app_candidates[0].get('guid')

    return appguid

def get_app_guid_from_legacy_id(app_id):
    app = Applications().get(legacy_id=app_id)
    if app is None:
        return
    return app['_embedded']['applications'][0]['guid']

def get_application_name(guid):
    app = Applications().get(guid)
    return app['profile']['name']

def get_findings_by_type(app_guid, scan_type='STATIC', sandbox_guid=None):
    max_retries = 15
    retry_count = 0
    while retry_count < max_retries:
        try:
            if scan_type == 'STATIC':
                return Findings().get_findings(app_guid,scantype=scan_type,annot='TRUE',sandbox=sandbox_guid)
            elif scan_type == 'DYNAMIC':
                return Findings().get_findings(app_guid,scantype=scan_type,annot='TRUE')
        except requests.RequestException as e:
            print(f"RequestException occurred: {e}")
            retry_count += 1
            print(f"Retrying...Attempt {retry_count}")
            time.sleep(1)  # Sleep for a short duration before retrying
    raise requests.RequestException("Max retries reached. Unable to get findings.")

def logprint(log_msg):
    log.info(log_msg)
    print(log_msg)

def filter_approved(findings,id_list):
    if id_list is not None:
        log.info('Only copying the following findings provided in id_list: {}'.format(id_list))
        findings = [f for f in findings if f['issue_id'] in id_list]

    return [f for f in findings if (f['finding_status']['resolution_status'] == 'APPROVED')]

def format_file_path(file_path):

    # special case - omit prefix for teamcity work directories, which look like this:
    # teamcity/buildagent/work/d2a72efd0db7f7d7
    if file_path is None:
        return ''

    suffix_length = len(file_path)

    buildagent_loc = file_path.find('teamcity/buildagent/work/')

    if buildagent_loc > 0:
        #strip everything starting with this prefix plus the 17 characters after
        # (25 characters for find string, 16 character random hash value, plus / )
        formatted_file_path = file_path[(buildagent_loc + 42):suffix_length]
    else:
        formatted_file_path = file_path

    return formatted_file_path

def create_match_format_policy(app_guid, sandbox_guid, policy_findings, finding_type):
    findings = []

    if finding_type == 'STATIC':
        thesefindings = [{'app_guid': app_guid,
                'sandbox_guid': sandbox_guid,
                'id': pf['issue_id'],
                'resolution': pf['finding_status']['resolution'],
                'cwe': pf['finding_details']['cwe']['id'],
                'procedure': pf['finding_details'].get('procedure'),
                'relative_location': pf['finding_details'].get('relative_location'),
                'source_file': format_file_path(pf['finding_details'].get('file_path')),
                'line': pf['finding_details'].get('file_line_number'),
                'finding': pf} for pf in policy_findings]
        findings.extend(thesefindings)
    elif finding_type == 'DYNAMIC':
        thesefindings = [{'app_guid': app_guid,
                'id': pf['issue_id'],
                'resolution': pf['finding_status']['resolution'],
                'cwe': pf['finding_details']['cwe']['id'],
                'path': pf['finding_details']['path'],
                'vulnerable_parameter': pf['finding_details'].get('vulnerable_parameter',''), # vulnerable_parameter may not be populated for some info leak findings
                'finding': pf} for pf in policy_findings]
        findings.extend(thesefindings)
    return findings

def format_application_name(guid, app_name, sandbox_guid=None):
    if sandbox_guid is None:
        formatted_name = 'application {} (guid: {})'.format(app_name,guid)
    else:
        formatted_name = 'sandbox {} in application {} (guid: {})'.format(sandbox_guid,app_name,guid)
    return formatted_name

def update_mitigation_info_rest(to_app_guid,flaw_id,action,comment,sandbox_guid=None, propose_only=False):
    # validate length of comment argument, gracefully handle overage
    if len(comment) > 2048:
        comment = comment[0:2048]

    if action == 'CONFORMS' or action == 'DEVIATES':
        log.warning('Cannot copy {} mitigation for Flaw ID {} in {}'.format(action,flaw_id,to_app_guid))
        return
    elif action == 'APPROVED':
        if propose_only:
            log.info('propose_only set to True; skipping applying approval for flaw_id {}'.format(flaw_id))
            return
        action = Constants.ANNOT_TYPE[action]
    elif action == 'CUSTOMCLEANSERPROPOSED' or action == 'CUSTOMCLEANSERUSERCOMMENT':
        log.warning(f"""Cannot copy '{action}' mitigation for Flaw ID {flaw_id} in {to_app_guid}""")
        return
    
    flaw_id_list = [flaw_id]
    try:
        if sandbox_guid==None:
            Findings().add_annotation(to_app_guid,flaw_id_list,comment,action)
        else:
            Findings().add_annotation(to_app_guid,flaw_id_list,comment,action,sandbox=sandbox_guid)
        log.info(
            'Updated mitigation information to {} for Flaw ID {} in {}'.format(action, str(flaw_id_list), to_app_guid))
    except requests.exceptions.RequestException as e:
        logprint(f"""WARNING: Unable to apply annotation '{action}' for Flaw ID {flaw_id_list} in {to_app_guid}""")
        log.exception('Ignoring request exception')

def set_in_memory_flaw_to_approved(findings_to,to_id):
    # use this function to update the status of target findings in memory, so that, if it is found
    # as a match for multiple flaws, we only copy the mitigations once.
    for finding in findings_to:
        if all (k in finding for k in ("id", "finding")):
            if (finding["id"] == to_id):
                finding['finding']['finding_status']['resolution_status'] = 'APPROVED'
def get_formatted_app_name(app_guid, sandbox_guid):
    app_name = get_application_name(app_guid)
    return format_application_name(app_guid,app_name,sandbox_guid)

def get_findings_from(from_app_guid, scan_type, from_sandbox_guid=None):
    formatted_app_name = get_formatted_app_name(from_app_guid, from_sandbox_guid)
    logprint('Getting {} findings for {}'.format(scan_type.lower(),formatted_app_name))
    findings_from = get_findings_by_type(from_app_guid,scan_type=scan_type, sandbox_guid=from_sandbox_guid)
    count_from = len(findings_from)
    logprint('Found {} {} findings in "from" {}'.format(count_from,scan_type.lower(),formatted_app_name))
    return findings_from

def match_for_scan_type(findings_from, from_app_guid, to_app_guid, dry_run, scan_type='STATIC',from_sandbox_guid=None,
        to_sandbox_guid=None, propose_only=False, id_list=[], fuzzy_match=False):
    if len(findings_from) == 0:
        return 0 # no source findings to copy!

    if len(filter_approved(findings_from,id_list)) == 0:
        logprint('No approved findings in "from" {}. Exiting.'.format(from_app_guid))
        return 0

    results_to_app_name = get_application_name(to_app_guid)
    formatted_to = format_application_name(to_app_guid,results_to_app_name,to_sandbox_guid)

    logprint('Getting {} findings for {}'.format(scan_type.lower(),formatted_to))
    findings_to = get_findings_by_type(to_app_guid,scan_type=scan_type, sandbox_guid=to_sandbox_guid)
    count_to = len(findings_to)
    logprint('Found {} {} findings in "to" {}'.format(count_to,scan_type.lower(),formatted_to))
    if count_to == 0:
        return 0 # no destination findings to mitigate!

    # CREATE LIST OF UNIQUE VALUES FOR BUILD COPYING TO
    copy_array_to = create_match_format_policy( app_guid=to_app_guid, sandbox_guid=to_sandbox_guid, policy_findings=findings_to,finding_type=scan_type)

    # We'll return how many mitigations we applied
    counter = 0

    formatted_from = get_formatted_app_name(from_app_guid, from_sandbox_guid)
    # look for a match for each finding in the TO list and apply mitigations of the matching flaw, if found
    for this_to_finding in findings_to:
        to_id = this_to_finding['issue_id']

        if this_to_finding['finding_status']['resolution_status'] == 'APPROVED':
            logprint ('Flaw ID {} in {} already has an accepted mitigation; skipped.'.format(to_id,formatted_to))
            continue

        match = Findings().match(this_to_finding,findings_from,approved_matches_only=True,allow_fuzzy_match=fuzzy_match)

        if match == None:
            log.info('No approved match found for finding {} in {}'.format(to_id,formatted_from))
            continue

        from_id = match.get('id')

        log.info('Source flaw {} in {} has a possible target match in flaw {} in {}.'.format(from_id,formatted_from,to_id,formatted_to))

        mitigation_list = match['finding']['annotations']
        logprint ('Applying {} annotations for flaw ID {} in {}...'.format(len(mitigation_list),to_id,formatted_to))

        for mitigation_action in reversed(mitigation_list): #findings API puts most recent action first
            proposal_action = mitigation_action['action']
            proposal_comment = '(COPIED FROM APP {}) {}'.format(from_app_guid, mitigation_action['comment'])
            if not(dry_run):
                update_mitigation_info_rest(to_app_guid, to_id, proposal_action, proposal_comment, to_sandbox_guid, propose_only)

        set_in_memory_flaw_to_approved(copy_array_to,to_id) # so we don't attempt to mitigate approved finding twice
        counter += 1

    print('[*] Updated {} flaws in {}. See log file for details.'.format(str(counter),formatted_to))

def get_exact_name_match(application_name, app_candidates):
    for application_candidate in app_candidates:
        if application_candidate["profile"]["name"] == application_name:
            return application_candidate["guid"]
    print("Unable to find application named " + application_name)
    return None

def get_application_by_name(application_name):
    app_candidates = Applications().get_by_name(application_name)
    if len(app_candidates) == 0:
        print("Unable to find application named " + application_name)
        return None
    elif len(app_candidates) > 1:
        return get_exact_name_match(application_name, app_candidates)
    else:
        return app_candidates[0].get('guid')
    
def get_sandbox_by_name(application_guid, sandbox_name):
    sandbox_candidates = Sandboxes().get_all(application_guid)
    for sandbox_candidate in sandbox_candidates:
        if sandbox_candidate["name"] == sandbox_name:
            return sandbox_candidate["guid"]
    print("Unable to find sandbox named " + sandbox_name + " for app with guid: " + application_guid)
    return None
    

def get_application_guids_by_name(application_names):
    application_ids = []
    names_as_list = [build.strip() for build in application_names.split(", ")]

    for application_name in names_as_list:
        application_id = get_application_by_name(application_name)
        if application_id is not None:
            application_ids.append(application_id)

    return application_ids

def parse_applications_csv(file_path):
    applications = []
    with open(file_path, newline='',encoding='utf-8-sig') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            application = {
                'Application Name': row['Applications Application Name'],
                'Sandbox Name': row['Scans Sandbox Name'],
            }
            applications.append(application)
    return applications


def main():
    parser = argparse.ArgumentParser(
        description='This script runs through a list of .'
    )
    parser.add_argument('-d', '--dry_run', action='store_true', help="Log matched flaws instead of applying mitigations")
    parser.add_argument('-fm','--fuzzy_match',action='store_true', help='Look within a range of line numbers for a matching flaw')
    parser.add_argument('-D', '--debug',action='store_true',help="Show debug information")
    args = parser.parse_args()

    setup_logger(args.debug)

    logprint('======== beginning StaticSelfCopier.py run ========')

    # CHECK FOR CREDENTIALS EXPIRATION
    creds_expire_days_warning()

    # SET VARIABLES FOR FROM AND TO APPS
    dry_run = args.dry_run
    fuzzy_match = args.fuzzy_match

    if dry_run:
        logprint("DRY RUN, not making any changes.")

    applications_data = parse_applications_csv('applications.csv')
    for app in applications_data:
        app_guid = get_application_by_name(app['Application Name'])

        if app_guid in ( None, '' ):
            print('Unable to match: ' + app['Application Name'])
            continue

        sandbox_guid = None
        if app['Sandbox Name'] != "Policy Sandbox":
            sandbox_guid = get_sandbox_by_name(app_guid, app['Sandbox Name'])

            if sandbox_guid in ( None, '' ):
                print('Unable to match sandbox name: ' + app['Sandbox Name'])
                continue

        all_static_findings = get_findings_from(
            from_app_guid=app_guid, 
            scan_type='STATIC',
            from_sandbox_guid=sandbox_guid,
        )

        match_for_scan_type(
            all_static_findings, 
            id_list=None,
            from_app_guid=app_guid,
            to_app_guid=app_guid,
            dry_run=dry_run, 
            scan_type='STATIC',
            from_sandbox_guid=sandbox_guid,
            to_sandbox_guid=sandbox_guid,
            fuzzy_match=fuzzy_match
        )

if __name__ == '__main__':
    main()
