# Copyright (C) 2020-2021 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Aggregator module."""

import base64
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from logging import getLogger
from pathlib import Path
from subprocess import call
from typing import Union

import requests
from click import confirm

logger = getLogger(__name__)

TOKEN_DELIMITER = '.'


def download_step_bin(
        url,
        grep_name,
        architecture,
        prefix: Union[Path, str] = '.',
        confirmation=True
) -> None:
    """
    Donwload step binaries from github.

    Args:
        url: address of latest release
        grep_name: name to grep over github assets
        architecture: architecture type to grep
        prefix: folder path to download
        confirmation: request user confirmation or not
    """
    if confirmation:
        confirm('CA binaries from github will be downloaded now', default=True, abort=True)
    result = requests.get(url)
    if result.status_code != 200:
        logger.warning('Can\'t download binaries from github. Please try lately.')
        return

    assets = result.json().get('assets', [])
    urls = [
        a['browser_download_url']
        for a in assets
        if grep_name in a['name'] and architecture in a['name']
    ]
    url = urls[-1]
    url = url.replace('https', 'http')
    name = url.split('/')[-1]
    logger.info(f'Downloading {name}')
    urllib.request.urlretrieve(url, f'{prefix}/{name}')
    shutil.unpack_archive(f'{prefix}/{name}', f'{prefix}/step')


def get_token(name, ca_url, ca_path='.'):
    """
    Create authentication token.

    Args:
        name: common name for following certificate
                    (aggregator fqdn or collaborator name)
        ca_url: full url of CA server
        ca_path: path to ca folder
    """
    ca_path = Path(ca_path)
    step_config_dir = ca_path / 'step_config'
    pki_dir = ca_path / 'cert'
    step_path = get_step_bin_path(ca_path)
    if not step_path:
        raise Exception('Step-CA is not installed!\nRun `fx pki install` first')

    priv_json = step_config_dir / 'secrets' / 'priv.json'
    pass_file = pki_dir / 'pass_file'
    root_crt = step_config_dir / 'certs' / 'root_ca.crt'
    try:
        token = subprocess.check_output(
            f'{step_path} ca token {name} '
            f'--key {priv_json} --root {root_crt} '
            f'--password-file {pass_file} 'f'--ca-url {ca_url}', shell=True)
    except subprocess.CalledProcessError as exc:
        logger.error(f'Error code {exc.returncode}: {exc.output}')
        return

    token = token.strip()
    token_b64 = base64.b64encode(token)

    with open(step_config_dir / 'certs' / 'root_ca.crt', mode='rb') as file:
        root_ca = file.read()
    root_ca_b64 = base64.b64encode(root_ca)

    return TOKEN_DELIMITER.join([
        token_b64.decode('utf-8'),
        root_ca_b64.decode('utf-8'),
    ])


def get_step_bin_path(ca_path: Union[Path, str]) -> Union[Path, None]:
    """Get path of step binary."""
    ca_path = Path(ca_path) / 'step'
    step_path = None
    if ca_path.exists():
        ca_last_version = sorted(os.listdir(ca_path))[-1]
        step_path = ca_path / ca_last_version / 'bin' / 'step'
    return step_path


def get_step_ca_bin_path(ca_path: Union[Path, str]) -> Union[Path, None]:
    """Get path of step-ca binary."""
    ca_path = Path(ca_path) / 'step'
    step_path = None
    if ca_path.exists():
        ca_last_version = sorted(os.listdir(ca_path))[-1]
        step_path = ca_path / ca_last_version / 'bin' / 'step-ca'
    return step_path


def certify(name, cert_path: Path, token_with_cert, ca_path: Path):
    """Create an envoy workspace."""
    os.makedirs(cert_path, exist_ok=True)

    token, root_ca = token_with_cert.split(TOKEN_DELIMITER)
    token = base64.b64decode(token).decode('utf-8')
    root_ca = base64.b64decode(root_ca)

    step_path = get_step_bin_path(ca_path)
    if not step_path:
        url = 'http://api.github.com/repos/smallstep/cli/releases/latest'
        download_step_bin(url, 'step_linux', 'amd', prefix=ca_path)
        step_path = get_step_bin_path(ca_path)
    if not step_path:
        raise Exception('Step-CA is not installed!\nRun `fx pki install` first')

    with open(f'{cert_path}/root_ca.crt', mode='wb') as file:
        file.write(root_ca)
    call(f'./{step_path} ca certificate {name} {cert_path}/{name}.crt '
         f'{cert_path}/{name}.key -f --token {token}', shell=True)


def remove_ca(ca_path):
    """Kill step-ca process and rm ca directory."""
    _check_kill_process('step-ca')
    shutil.rmtree(ca_path, ignore_errors=True)


def install(ca_path, ca_url, password):
    """
    Create certificate authority for federation.

    Args:
        ca_path: path to ca directory
        ca_url: url for ca server like: 'host:port'
        password: Simple password for encrypting root private keys

    """
    logger.info('Creating CA')

    ca_path = Path(ca_path)
    ca_path.mkdir(parents=True, exist_ok=True)
    step_config_dir = ca_path / 'step_config'
    os.environ['STEPPATH'] = str(step_config_dir)
    step_path = get_step_bin_path(ca_path)
    step_ca_path = get_step_ca_bin_path(ca_path)

    if not (step_path and step_ca_path and step_path.exists() and step_ca_path.exists()):
        confirm('CA binaries from github will be downloaded now', default=True, abort=True)
        url = 'http://api.github.com/repos/smallstep/certificates/releases/latest'
        download_step_bin(url, 'step-ca_linux', 'amd', prefix=ca_path, confirmation=False)
        url = 'http://api.github.com/repos/smallstep/cli/releases/latest'
        download_step_bin(url, 'step_linux', 'amd', prefix=ca_path, confirmation=False)
    step_config_dir = ca_path / 'step_config'
    if (not step_config_dir.exists()
            or confirm('CA exists, do you want to recreate it?', default=True)):
        _create_ca(ca_path, ca_url, password)
    _configure(step_config_dir)


def run_ca(step_ca, pass_file, ca_json):
    """Run CA server."""
    if _check_kill_process('step-ca', confirmation=True):
        logger.info('Up CA server')
        call(f'{step_ca} --password-file {pass_file} {ca_json}', shell=True)


def _check_kill_process(pstring, confirmation=False):
    """Kill process by name."""
    pids = []
    proc = subprocess.Popen(f'ps ax | grep {pstring} | grep -v grep',
                            shell=True, stdout=subprocess.PIPE)
    text = proc.communicate()[0].decode('utf-8')

    for line in text.splitlines():
        fields = line.split()
        pids.append(fields[0])

    if len(pids):
        if confirmation and not confirm('CA server is already running. Stop him?', default=True):
            return False
        for pid in pids:
            os.kill(int(pid), signal.SIGKILL)
        time.sleep(2)
    return True


def _create_ca(ca_path: Path, ca_url: str, password: str):
    """Create a ca workspace."""
    pki_dir = ca_path / 'cert'
    step_config_dir = ca_path / 'step_config'

    pki_dir.mkdir(parents=True, exist_ok=True)
    step_config_dir.mkdir(parents=True, exist_ok=True)

    with open(f'{pki_dir}/pass_file', 'w') as f:
        f.write(password)
    step_path = get_step_bin_path(ca_path)
    step_ca_path = get_step_ca_bin_path(ca_path)
    assert (step_path and step_ca_path and step_path.exists() and step_ca_path.exists())

    logger.info('Create CA Config')
    os.environ['STEPPATH'] = str(step_config_dir)
    shutil.rmtree(step_config_dir, ignore_errors=True)
    name = ca_url.split(':')[0]
    call(f'{step_path} ca init --name name --dns {name} '
         f'--address {ca_url}  --provisioner prov '
         f'--password-file {pki_dir}/pass_file', shell=True)

    call(f'{step_path} ca provisioner remove prov --all', shell=True)
    call(f'{step_path} crypto jwk create {step_config_dir}/certs/pub.json '
         f'{step_config_dir}/secrets/priv.json --password-file={pki_dir}/pass_file', shell=True)
    call(
        f'{step_path} ca provisioner add provisioner {step_config_dir}/certs/pub.json',
        shell=True
    )


def _configure(step_config_dir):
    conf_file = step_config_dir / 'config' / 'ca.json'
    with open(conf_file, 'r+') as f:
        data = json.load(f)
        data.setdefault('authority', {}).setdefault('claims', {})
        data['authority']['claims']['maxTLSCertDuration'] = f'{365 * 24}h'
        data['authority']['claims']['defaultTLSCertDuration'] = f'{365 * 24}h'
        data['authority']['claims']['maxUserSSHCertDuration'] = '24h'
        data['authority']['claims']['defaultUserSSHCertDuration'] = '24h'
        f.seek(0)
        json.dump(data, f, indent=4)
        f.truncate()