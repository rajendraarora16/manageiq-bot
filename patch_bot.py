#!/usr/bin/env python
# -*- coding: utf-8 -*-
#   manageiq-bot
#   Copyright(C) 2017 Christoph Görn
#
#   This program is free software: you can redistribute it and / or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see < http: // www.gnu.org / licenses / >.

"""This is Thoth, a dependency updating bot for the ManageIQ community.
"""

__version__ = '0.3.0'

import os
import json
import logging
import shutil
import fileinput
from pathlib import Path

import semver
from tornado import httpclient
from gemfileparser import GemfileParser

from git import Repo, Actor
from git.exc import GitCommandError
from github import Github

from github_helper import Checklist, pr_in_progress

from configuration import *

DEBUG_LOG_LEVEL = bool(os.getenv('DEBUG', False))

if DEBUG_LOG_LEVEL:
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s,%(levelname)s,%(filename)s:%(lineno)d,%(message)s')
else:
    logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name



def major(semverlike):
    _semver = None
    try:
        _semver = semver.parse(semverlike)['major']
    except ValueError as ve:
        logging.error("%s: %s" %(ve, semverlike))
        _semver = semverlike.split('.')[0]

    return _semver


def minor(semverlike):
    _semver = None
    try:
        _semver = semver.parse(semverlike)['minor']
    except ValueError as ve:
        logging.error("%s: %s" % (ve, semverlike))
        _semver = semverlike.split('.')[1]

    return _semver


def get_dependency_status(slug):
    http_client = httpclient.HTTPClient()

    try:
        req = httpclient.HTTPRequest(url=GEMNASIUM_STATUS_URL,
                                     auth_username='X',
                                     auth_password=os.getenv('GEMNASIUM_API_KEY'))
        response = http_client.fetch(req)

        with open('gemnasium-manageiq.json', 'w') as file:
            file.write(response.body.decode("utf-8"))
    except httpclient.HTTPError as e:
        # HTTPError is raised for non-200 responses; the response
        # can be found in e.response.
        logging.error(e)
    except Exception as e:
        # Other errors are possible, such as IOError.
        logging.error(e)
    http_client.close()

    # TODO handle exceptions
    data = json.load(open('gemnasium-manageiq.json'))

    return data


def update_minor_dependency(package):
    """update_minor_dependency is a trivial approach to update the given package to it's current stable release.
    This release will be locked in the Gemfile"""

    logging.info("updating %s to %s" % (package['name'],
                                        package['distributions']['stable']))

    OWD = os.getcwd()
    os.chdir(LOCAL_WORK_COPY)

    parser = GemfileParser('Gemfile', 'manageiq')
    deps = parser.parse()

    # lets loop thru all dependencies and if we found the thing we wanna change
    # open the Gemfile, walk thru all lines and change the thing
    for key in deps:
        if deps[key]:
            for dependency in deps[key]:
                if dependency.name == package['name']:
                    with fileinput.input(files=('Gemfile'), inplace=True, backup='.swp') as Gemfile:
                        for line in Gemfile:
                            if '"' + dependency.name + '"' in line:
                                line = line.replace(dependency.requirement,
                                                    package['distributions']['stable'], 1)

                            print(line.replace('\n','',1)) # no new line!

    os.chdir(OWD)


def cleanup(directory):
    """clean up the mess we made..."""
    logging.info("Cleaning up workdir: %s" % directory)

    try:
        shutil.rmtree(directory)
    except FileNotFoundError as fnfe:
        logging.info("Non Fatal Error: " + str(fnfe))


if __name__ == '__main__':
    logging.debug("using ssh command: {}".format(SSH_CMD))

    if not GITHUB_ACCESS_TOKEN:
        logging.error("No GITHUB_ACCESS_TOKEN")
        exit(-1)

    g = Github(login_or_token=GITHUB_ACCESS_TOKEN)
    logging.info("I am " + g.get_user().name)
    
    repo = g.get_repo(TRAVIS_REPO_SLUG)
    bot_label = repo.get_label('bot')

    tasks_list = Checklist("The following dependencies need minor updates:")

    # and request current status from gemnasium
    deps = get_dependency_status('goern/manageiq')

    # check if gemnasium is up to date...
    if not deps[0]['requirement']:
        logging.debug(deps[0])
        logging.error('Gemnasium is outdated...')
        exit(-1)

    # clone our github repository
    cleanup(LOCAL_WORK_COPY)
    try:
        logging.info("Cloning git repository %s to %s" %
                     (MASTER_REPO_URL_RO, LOCAL_WORK_COPY))
        repository = Repo.clone_from(MASTER_REPO_URL_RO, LOCAL_WORK_COPY)
    except GitCommandError as git_error:
        logging.error(git_error)
        exit(-1)

    # lets have a look at all dependencies
    for dep in deps:
        if dep['color'] == 'yellow': # and at first, just the yellow ones
            logging.debug(dep)
            # if we have no major version shift, lets update the Gemfile
            if major(dep['requirement'].split(' ', 1)[1]) == major(dep['package']['distributions']['stable']):
                target_branch = 'bots-life/updating-' + dep['package']['name']

                if not pr_in_progress(g, TRAVIS_REPO_SLUG, target_branch):
                    # 1. create a new branch
                    new_branch = repository.create_head(target_branch)
                    new_branch.checkout()

                    # 2. update Gemfile and Gemfile.lock
                    update_minor_dependency(dep['package'])

                    # 3. commit work
 
                    repository.index.add(['Gemfile'])
                    author = Actor('Thoth Dependency Bot',
                                   'goern+sesheta@redhat.com')
                    committer = Actor('Thoth Dependency Bot',
                                      'goern+sesheta@redhat.com')
                    repository.index.commit('Updating {} from {} to {}'.format(dep['package']['name'], 
                                                                                dep['requirement'],
                                                                                dep['package']['distributions']['stable']),
                                            author=author, committer=committer)

                    tasks_list.add('{} from {} to {}'.format(dep['package']['name'],
                                                             dep['requirement'],
                                                             dep['package']['distributions']['stable']))

                    # 4. push to origin
                    with repository.git.custom_environment(GIT_SSH_COMMAND=SSH_CMD):
                        repository.remotes.origin.push(refspec='{}:{}'.format(
                            target_branch, target_branch))

                    # 5. checkout master
                    repository.refs.master.checkout()
                else:
                    logging.info("There is an open PR for %s, aborting..." %
                        (target_branch))               

            else:
                logging.info("NOT updating %s %s -> %s" % (dep['package']['name'],
                                                  dep['requirement'],
                                                  dep['package']['distributions']['stable']))

    # 6. open update issue
    repo.create_issue('[Thoth] proposing minor updates',
                      body=tasks_list.body, labels=[bot_label])
