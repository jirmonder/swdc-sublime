# Copyright (c) 2018 by Software.com

from datetime import datetime
from threading import Thread, Timer, Event
import webbrowser
import uuid
import time
import json
import os
import sublime_plugin, sublime
from .SoftwareHttp import *
from .SoftwareUtil import *

# Constants
DASHBOARD_KEYMAP_MSG = "Log in to Software.com [ctrl+alt+o]"
SECONDS_PER_HOUR = 60 * 60
LONG_THRESHOLD_HOURS = 12
SHORT_THRESHOLD_HOURS = 4
NO_TOKEN_THRESHOLD_HOURS = 2
LOGIN_LABEL = "Log in"

fetchingUserFromToken = False
fetchingKpmData = False

# launch the browser with either the dashboard or the login
def launchDashboard():
    sublime_settings = sublime.load_settings("Software.sublime-settings")
    webUrl = sublime_settings.get("software_dashboard_url", "https://app.software.com")
    log("web url %s" % webUrl)
    existingJwt = getItem("jwt")
    if (existingJwt is None):
        tokenVal = getItem("token")
        log("existing token val: %s" % tokenVal)
        if (tokenVal is None):
            tokenVal = createToken()
            log("token val after creation: %s" % tokenVal)
            # update the .software data with the token we've just created
            setItem("token", tokenVal)
        webUrl += "/onboarding?token=" + tokenVal

    webbrowser.open(webUrl)

# store the payload offline
def storePayload(payload):
    # append payload to software data store file
    dataStoreFile = getSoftwareDataStoreFile()

    with open(dataStoreFile, "a") as dsFile:
        dsFile.write(payload + "\n")

def checkOnline():
    # non-authenticated ping, no need to set the Authorization header
    response = requestIt("GET", "/ping", None)
    if (response is not None and int(response.status) < 300):
        return True
    else:
        return False

# send the data that has been saved offline
def sendOfflineData():
    existingJwt = getItem("jwt")

    # no need to try to send the offline data if we don't have an auth token
    if (existingJwt is None):
        return

    # send the offline data
    dataStoreFile = getSoftwareDataStoreFile()

    payloads = []

    try:
        with open(dataStoreFile) as fp:
            for line in fp:
                if (line and line.strip()):
                    line = line.rstrip()
                    # convert to object
                    json_obj = json.loads(line)
                    # convert to json to send
                    payloads.append(json_obj)
    except FileNotFoundError:
        log("%s file not found" % dataStoreFile)
    except Exception:
        log("Unexpected error reading file %s" % dataStoreFile)

    if (payloads):
        response = requestIt("POST", "/data/batch", json.dumps(payloads))

        if (response is not None):
            os.remove(dataStoreFile)

def chekUserAuthenticationStatus():
    serverAvailable = checkOnline()
    authenticated = isAuthenticated()
    pastThresholdTime = isPastTimeThreshold()
    existingJwt = getItem("jwt")
    existingToken = getItem("token")

    initiateCheckTokenAvailability = True

    # show the dialog if we don't have a token yet,
    # or if we do have a token but no jwt token then
    # show it every 4 hours until we get a jwt token

    if (serverAvailable and not authenticated and pastThresholdTime):

        # remove the jwt so we can re-establish a connection since we're not authenticated
        setItem("jwt", None)

        # set the last update time so we don't try to ask too frequently
        setItem("sublime_lastUpdateTime", int(trueSecondsNow()))
        confirmWindowOpen = True
        infoMsg = "To see your coding data in Software.com, please log in to your account."
        if (existingToken is not None and existingJwt):
            # show the Software.com message
            showStatus(DASHBOARD_KEYMAP_MSG)
        else:
            clickAction = sublime.ok_cancel_dialog(infoMsg, LOGIN_LABEL)
            if (clickAction):
                # launch the login view
                launchDashboard()
    elif (not authenticated):
        # show the Software.com message
        showStatus(DASHBOARD_KEYMAP_MSG)
    else:
        initiateCheckTokenAvailability = False

    existingToken = getItem("token")
    if (existingToken is not None and initiateCheckTokenAvailability is True):
        # start the token availability timer
        tokenAvailabilityTimer = Timer(60, checkTokenAvailability)
        tokenAvailabilityTimer.start()

def isAuthenticated():
    tokenVal = getItem('token')
    jwtVal = getItem('jwt')

    if (tokenVal is None or jwtVal is None):
        showStatus(DASHBOARD_KEYMAP_MSG)
        return False

    response = requestIt("GET", "/users/ping", None)

    if (response is not None and int(response.status) < 300):
        return True
    else:
        showStatus(DASHBOARD_KEYMAP_MSG)
        return False

# check if we can update the user if they need to authenticate or not
def isPastTimeThreshold():
    existingJwt = getItem('jwt')

    thresholdHoursBeforeCheckingAgain = LONG_THRESHOLD_HOURS
    if (existingJwt is None):
        existingToken = getItem("token")
        if (existingToken is None):
            thresholdHoursBeforeCheckingAgain = NO_TOKEN_THRESHOLD_HOURS
        else:
            thresholdHoursBeforeCheckingAgain = SHORT_THRESHOLD_HOURS

    lastUpdateTime = getItem("sublime_lastUpdateTime")
    if (lastUpdateTime is None):
        lastUpdateTime = 0

    timeDiffSinceUpdate = trueSecondsNow() - lastUpdateTime

    threshold = SECONDS_PER_HOUR * thresholdHoursBeforeCheckingAgain

    if (timeDiffSinceUpdate < threshold):
        return False

    return True

#
# check if the token is found to establish an authenticated session
#
def checkTokenAvailability():
    global fetchingUserFromToken

    tokenVal = getItem("token")
    jwtVal = getItem("jwt")

    foundJwt = False
    if (tokenVal is not None):
        api = '/users/plugin/confirm?token=' + tokenVal
        response = requestIt("GET", api, None)

        if (response is not None and int(response.status) < 300):

            json_obj = json.loads(response.read().decode('utf-8'))

            jwt = json_obj.get("jwt", None)
            user = json_obj.get("user", None)
            if (jwt is not None):
                setItem("jwt", jwt)
                setItem("user", user)
                setItem("sublime_lastUpdateTime", int(trueSecondsNow()))
                foundJwt = True
            else:
                # check if there's a message
                message = json_obj.get("message", None)
                if (message is not None):
                    log("Software.com: Failed to retrieve session token, reason: \"%s\"" % message)
        elif (response is not None and int(response.status) == 400):
            setItem("jwt", None)
            setItem("token", None)

    if (not foundJwt and foundJwt is None):
        # start the token availability timer again
        tokenAvailabilityTimer = Timer(120, checkTokenAvailability)
        tokenAvailabilityTimer.start()
        showStatus(DASHBOARD_KEYMAP_MSG)

#
# Fetch and display the daily KPM info
#
def fetchDailyKpmSessionInfo():
    global fetchingKpmData

    if (fetchingKpmData is False):

        if (isAuthenticated()):
            fetchingKpmData = True
            api = '/sessions?from=' + str(int(trueSecondsNow())) + '&summary=true'
            response = requestIt("GET", api, None)

            fetchingKpmData = False

            if (response is not None and int(response.status) < 300):
                sessions = json.loads(response.read().decode('utf-8'))

                avgKpm = '{:1.0f}'.format(sessions.get("kpm", 0))
                totalMin = sessions.get("minutesTotal", 0)
                sessionTime = ""
                inFlow = sessions.get("inFlow", False)

                if (totalMin == 60):
                    sessionTime = "1 hr"
                elif (totalMin > 60):
                    # at least 4 chars (including the dot) with 2 after the dec point
                    sessionTime = '{:4.2f}'.format((totalMin / 60)) + " hrs"
                elif (totalMin == 1):
                    sessionTime = "1 min"
                else:
                    sessionTime = '{:1.0f}'.format(totalMin) + " min"

                statusMsg = avgKpm + " KPM, " + sessionTime

                if (totalMin > 0 or avgKpm > 0):
                    if (inFlow):
                        # set the status bar message
                        showStatus("<s> " + statusMsg + " ^")
                    else:
                        showStatus("<s> " + statusMsg)
                else:
                    showStatus(DASHBOARD_KEYMAP_MSG)
        else:
            chekUserAuthenticationStatus()

    # fetch the daily kpm session info in 1 minute
    kpmReFetchTimer = Timer(60, fetchDailyKpmSessionInfo)
    kpmReFetchTimer.start()

# crate a uuid token to establish a connection
def createToken():
    # return os.urandom(16).encode('hex')
    uid = uuid.uuid4()
    return uid.hex

def handlKpmClickedEvent():
    launchDashboard()



