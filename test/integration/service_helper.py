import hashlib
import hmac

import httplib2
import simplejson as json


def request(url, method, **kw):
    """
    returns:
        response, content
    """
    # print 'url: %s' % url
    headers = kw.get('headers', None)
    body = kw.get('body', None)
    
    # these are popped since they can't be passed to Http.request()
    user = kw.pop('user', None)
    passwd = kw.pop('passwd', None)
    
    h = httplib2.Http()
    if user and passwd:
        h.add_credentials(user, passwd)
    return h.request(url, method, **kw)

def verify_response(test_case, status, response, content):
    if response.status != status:
        test_case.fail("Unexpected response: %s %s (%s)" % (response.status, response.reason, content))
    
def request_and_verify(test_case, status, url, method, **args):
    response, content = request(url, method, **args)
    if response.status != status:
        test_case.fail("%s %s - expected %s - received %s %s\n%s\n%s" %
            (method, url, status, response.status, response.reason, response, content))

    return response, content
    
def parse_json(json_string):
    try:
        return json.loads(json_string)
    except json.JSONDecodeError, e:
        raise Exception("Malformed JSON: %s" % json_string)

def api_login_headers():
    """ returns the user authenticated to use the instinct API """
    return local_login_headers(user="meh.subterfusion@gmail.com", admin=False)
    
def local_login_headers(**kw):
    """
    Returns login headers (cookies) for login to the GAE development server.
    
    keywords:
        user: username
        admin: True or False
    """
    user = kw.get("user", "instinct.testing@subterfusion.net")
    admin = kw.get("admin", False)
    return { "Cookie": "dev_appserver_login=%s:%s:185804764220139124118" % (user, admin) }

def create_put_wrapper(test_case, url, operation, **kw):
    """
    handles artifact creation and deletion.
    
    params:
        operation - a method that is inject with the URL of a new artifact.
    """
    response, content = request_and_verify(test_case, 204, url, "PUT", **kw)
    try:
        operation(url, **kw)
    finally:
        # TODO: test deletes
        request_and_verify(test_case, 204, url, "DELETE", **kw)