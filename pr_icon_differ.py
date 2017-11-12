import sys
import getopt
import os
import re
import hmac
import json
import logging
from hashlib import sha1
import requests
from twisted.web import resource, server
from twisted.internet import reactor, endpoints

import icons

DEBUG = False

#Setup logging
log_format = "[%(asctime)s]: %(message)s"
logging_level = logging.INFO
if DEBUG:
    logging_level = logging.NOTSET
logging.basicConfig(
    filename='events.log',
    level=logging_level,
    format=log_format
)

console = logging.StreamHandler()
console.setLevel(logging_level)
console.setFormatter(logging.Formatter(log_format))
logging.getLogger('').addHandler(console)

logger = logging.getLogger(__name__)

def log_message(message):
    """Logs a message to a file and prints on screen"""
    logger.info(message)

#Setup the config
config = {}
if os.path.exists(os.path.abspath('config.json')):
    with open('config.json', 'r') as f:
        config = json.load(f)
else:
    log_message("Make sure the config file exists.")

github_secret = config['github']['secret'].encode('utf-8')
github_user = config['github']['user']
github_auth = config['github']['auth']
upload_api_url = config['upload_api']['url']
upload_api_key = config['upload_api']['key']

actions_to_check = ['opened', 'synchronize']
binary_regex = re.compile('diff --git a\/(.*\.dmi) b\/.?')

def compare_secret(secret_to_compare, payload):
    """Compares given secret with ours"""
    if secret_to_compare is None:
        return False

    this_secret = hmac.new(github_secret, payload, sha1)
    secret_to_compare = secret_to_compare.replace('sha1=', '')
    return hmac.compare_digest(secret_to_compare, this_secret.hexdigest())

def check_diff(diff_url):
    """Checks the diff url for icons"""
    req = requests.get(diff_url)
    if req.status_code == 404:
        return None
    diff = req.text.split('\n')
    icons_with_diff = []
    for line in diff:
        match = binary_regex.search(line)
        if not match:
            continue
        icons_with_diff.append(match.group(1))
    return icons_with_diff

def upload_image(file_to_upload, upload = True):
    """Uploads an image to the configured host"""
    if not upload:
        return None
    data = {'key' : upload_api_key}
    files = {'file' : file_to_upload}
    req = requests.post(upload_api_url, data=data, files=files)
    req_json = req.json()
    return req_json.get('url')

def post_comment(issue_url, message_dict):
    """Post a comment on given github issue url"""
    github_api_url = "{issue}/comments".format(issue=issue_url)
    body = json.dumps({'body' : '\n'.join(message_dict)})
    req = requests.post(github_api_url, data=body, auth=(github_user, github_auth))
    if req.status_code == 201:
        log_message("Sucessefully commented icon diff on: {}".format(issue_url))
    else:
        log_message("Failed to comment on: {}".format(issue_url))
        log_message("Error code: {}".format(req.status_code))

def check_icons(icons_with_diff, base, head, issue_url, send_message=True):
    """
    Checks two icons for their states, comparing the images and posting on the PR in case
    a diff exists
    """
    if not os.path.exists('./icon_dump'):
        os.makedirs('./icon_dump')
    base_repo_url = base.get('repo').get('html_url')
    head_repo_url = head.get('repo').get('html_url')
    msgs = ["Icons with diff:"]
    req_data = {'raw' : 1}
    if DEBUG:
        issue_number = re.sub('.*\/issues\/(\d*)', '\\1', issue_url)
    for icon in icons_with_diff:
        i_name = re.sub('.*\/(.*)\.dmi', '\\1', icon)
        icon_path_a = './icon_dump/old_{}.dmi'.format(i_name)
        icon_path_b = './icon_dump/new_{}.dmi'.format(i_name)
        response_a = requests.get('{}/blob/{}/{}'.format(base_repo_url, base['ref'], icon), data=req_data)
        response_b = requests.get('{}/blob/{}/{}'.format(head_repo_url, head['ref'], icon), data=req_data)
        if response_a.status_code == 200:
            with open(icon_path_a, 'wb') as f:
                f.write(response_a.content)
        elif response_a.status_code == 404:
            icon_path_a = None
        if response_b.status_code == 200:
            with open(icon_path_b, 'wb') as f:
                f.write(response_b.content)
        #This means the file is being deleted, which does not interest us
        elif response_b.status_code == 404:
            try:
                os.remove(icon_path_a)
                continue
            except OSError:
                continue
        this_dict = icons.compare_two_icon_files(icon_path_a, icon_path_b)
        if not this_dict:
            continue
        msg = ["<details><summary>{}</summary>\n".format(icon), "Key | Old | New | Status", "--- | --- | --- | ---"]
        for key in this_dict:
            status = this_dict[key].get("status")
            if status == 'Equal':
                continue
            path_a = './icon_dump/old_{}.png'.format(key)
            path_b = './icon_dump/new_{}.png'.format(key)
            img_a = this_dict[key].get('img_a')
            if img_a:
                img_a.save(path_a)
                with open(path_a, 'rb') as f:
                    url_a = "![{key}]({url})".format(key=key, url=upload_image(f, send_message))
                if not DEBUG:
                    os.remove(path_a)
            else:
                url_a = "![]()"
            img_b = this_dict[key].get('img_b')
            if img_b:
                img_b.save(path_b)
                with open(path_b, 'rb') as f:
                    url_b = "![{key}]({url})".format(key=key, url=upload_image(f, send_message))
                if not DEBUG:
                    os.remove(path_b)
            else:
                url_b = "![]()"
            msg.append("{key}|{url_a}|{url_b}|{status}".format(key=key, url_a=url_a, url_b=url_b, status=status))
        msg.append("</details>")
        msgs.append("\n".join(msg))
        if DEBUG:
            with open("icon_dump/{}_{}.log".format(i_name, issue_number), 'w') as fp:
                fp.write("\n".join(msg))
    if send_message and len(msgs) > 1:
        post_comment(issue_url, msgs)
    if os.path.exists(icon_path_a) and not DEBUG:
        os.remove(icon_path_a)
    if os.path.exists(icon_path_b) and not DEBUG:
        os.remove(icon_path_b)

class Handler(resource.Resource):
    """Opens a web server to handle POST requests on given port"""
    isLeaf = True
    def render_POST(self, request):
        payload = request.content.getvalue()
        if not compare_secret(request.getHeader('X-Hub-Signature'), payload):
            request.setResponseCode(401)
            log_message("POST received with wrong secret.")
            return b"Secret does not match."
        event = request.getHeader('X-GitHub-Event')
        if event != 'pull_request':
            request.setResponseCode(404)
            log_message("POST received with event: {}".format(event))
            return b"Event not supported"

        #Then we check the PR for icon diffs
        payload = json.loads("".join(map(chr, payload)))
        request.setResponseCode(200)
        pr_obj = payload['pull_request']
        if payload['action'] not in actions_to_check:
            return b"Not actionable"
        issue_url = pr_obj['issue_url']
        pr_diff_url = pr_obj['diff_url']
        head = pr_obj['head']
        base = pr_obj['base']
        if payload['action'] == 'synchronize':
            pr_diff_url = "{html_url}/commits/{sha}.patch".format(html_url=pr_obj['html_url'], sha=head['sha'])
        icons_with_diff = check_diff(pr_diff_url)
        if icons_with_diff:
            log_message("{}: Icon diff detected on pull request: {}!".format(head['repo']['full_name'], payload['number']))
            check_icons(icons_with_diff, base, head, issue_url)
        return b"Ok"
    def render_GET(self, request):
        request.setResponseCode(404)
        log_message("Received a GET request.")
        return b"GET requests are not supported."

def test_pr(number, owner, repository, send_message = False):
    """tests a pr for the icon diff"""
    req = requests.get("https://api.github.com/repos/{}/{}/pulls/{}".format(owner, repository, number))
    if req.status_code == 404:
        log_message('PR #{} on {}/{} does not exist.'.format(number, owner, repository))
        return
    payload = req.json()
    icons_diff = check_diff(payload['diff_url'])
    print(icons_diff)
    log_message("Icons:")
    log_message("\n".join(icons_diff))
    check_icons(icons_diff, payload['base'], payload['head'], payload['issue_url'], send_message)

def y_n_test(input_msg):
    if input_msg[:1] in ("Y", "y"):
        return True
    return False

def start_server():
    """Starts the webserver"""
    webhook_port = "tcp:{}".format(config['webhook_port'])
    endpoints.serverFromString(reactor, webhook_port).listen(server.Site(Handler()))
    log_message("Listening for requests on port: {}".format(config['webhook_port']))
    try:
        reactor.run()
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    if "debug" in sys.argv:
        DEBUG = True
        owner = input("Owner: ")
        repo = input("Repo: ")
        number = input("PR number: ")
        send_msg = y_n_test(input("Send message(y/n): "))
        test_pr(number, owner, repo, send_msg)
    else:
        start_server()
        