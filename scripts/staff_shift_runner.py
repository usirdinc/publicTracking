"""Attended four-window morning shift. Staff solve every official CAPTCHA."""
import argparse
import multiprocessing
import os
from pathlib import Path
import re
import signal
import sys
import time

import httpx
from playwright.sync_api import Error, sync_playwright
from scripts.staff_lookup_runner import prepare_ice, prepare_eoir, current_host
from tracker.eoir import _captcha_token
from tracker.staff_results import parse_result

BASE='https://track.usird.org'
SITES={'ice':'https://locator.ice.gov/odls/','eoir':'https://acis.eoir.justice.gov/en/'}


class ShiftClient:
    def __init__(self):
        credential=Path.home()/'case-tracker-operator'/'lookup-token'
        if credential.stat().st_mode & 0o077:raise RuntimeError('Operator credential must be owner-only.')
        token=credential.read_text(encoding='ascii').strip()
        self.client=httpx.Client(base_url=BASE,timeout=20,follow_redirects=False)
        response=self.client.post('/staff/lookups/session',headers={'Authorization':'Bearer '+token})
        if response.status_code!=204:raise RuntimeError('Machine sign-in failed; no lookup started.')
        response=self.client.get('/staff/lookups/shift')
        if response.status_code!=200:raise RuntimeError('Shift API is unavailable; deploy the matching server release first.')
        self.csrf=response.json()['csrf']

    def claim(self,worker,service):
        response=self.client.post('/staff/lookups/shift/claim',data={'csrf':self.csrf,'worker':worker,'service':service})
        if response.status_code!=200:raise RuntimeError('Assignment request failed.')
        return response.json()['job']

    def result(self,job,action,text='',url=''):
        response=self.client.post('/staff/lookups/shift/result',data={'csrf':self.csrf,'lease_token':job['lease_token'],
            'action':action,'page_text':text,'source_url':url})
        if response.status_code!=200:raise RuntimeError('Assignment expired, changed, or could not be saved.')
        return response.json()['outcome']


def wait_for_result(page,job,api,timeout):
    end=time.monotonic()+timeout
    heartbeat_at=time.monotonic()+45
    resubmitted=False
    while time.monotonic()<end:
        if page.is_closed():return None
        if not current_host(page,SITES[job['service']]):return None
        text=page.locator('body').inner_text(timeout=5000)
        result=parse_result(job['service'],text,page.url,job['number'])
        if result:return text,page.url
        if re.search(r'captcha error|unable to validate|access denied',text,re.I):return None
        if time.monotonic()>heartbeat_at:
            api.result(job,'heartbeat')
            heartbeat_at=time.monotonic()+45
        # Never solve or inject a CAPTCHA response. At most one ordinary submit
        # follows a token produced by the staff member in this same page.
        if not resubmitted and _captcha_token(page):
            page.wait_for_timeout(1800)
            if job['service']=='eoir':
                button=page.get_by_role('button',name=re.compile(r"^submit$",re.I))
            else:button=page.get_by_role('button',name='Search by A-Number',exact=True)
            if button.count()==1 and button.is_visible() and button.is_enabled():button.click(timeout=3000)
            resubmitted=True
        page.wait_for_timeout(500)
    return None


def worker(index,service,timeout,hours):
    name='beacon4-window-'+str(index)
    width,height=1920,1080
    try:
        import subprocess
        geometry=subprocess.check_output(['xrandr','--current'],stderr=subprocess.DEVNULL,text=True,timeout=5)
        dimensions=re.search(r'current (\d+) x (\d+)',geometry)
        if dimensions:width,height=map(int,dimensions.groups())
    except (OSError,ValueError,subprocess.SubprocessError):pass
    window_width=max(480,width//2)
    window_height=max(400,height//2)
    x=((index-1)%2)*window_width
    y=((index-1)//2)*window_height
    api=None
    job=None
    try:
        api=ShiftClient()
        stop_at=time.monotonic()+hours*3600
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=False,args=[f'--window-size={window_width},{window_height}',f'--window-position={x},{y}'])
            context=browser.new_context(viewport=None)
            page=context.new_page()
            idle_since=time.monotonic()
            try:
                while time.monotonic()<stop_at and not page.is_closed():
                    job=api.claim(name,service)
                    if not job:
                        if time.monotonic()-idle_since>60:break
                        print(name+': no unassigned due job; waiting.',flush=True)
                        time.sleep(5)
                        continue
                    idle_since=time.monotonic()
                    print(name+': '+job['service'].upper()+' lookup ready. Solve the CAPTCHA in this window.',flush=True)
                    try:
                        page.goto(SITES[job['service']],wait_until='domcontentloaded',timeout=30000)
                        if not current_host(page,SITES[job['service']]):raise ValueError('Unexpected official host')
                        if job['service']=='ice':prepare_ice(page,job['number'],job['country'])
                        else:prepare_eoir(page,job['number'],job['country'])
                        outcome=wait_for_result(page,job,api,timeout)
                        if outcome:status=api.result(job,'save',*outcome)
                        else:status=api.result(job,'retry')
                        print(name+': '+status+'.',flush=True)
                    except (Error,ValueError,RuntimeError):
                        # No body, identifier, token, or exception values in logs.
                        try:api.result(job,'retry')
                        except Exception:pass
                        print(name+': needs retry or staff review; previous result preserved.',flush=True)
                    job=None
                    time.sleep(3)
            finally:
                browser.close()
    except KeyboardInterrupt:pass
    except Exception:
        print(name+': stopped safely; check machine sign-in, connectivity, and matching release.',flush=True)
    finally:
        if api:
            if job:
                try:api.result(job,'retry')
                except Exception:pass
            api.client.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers',type=int,choices=range(1,5),default=4)
    parser.add_argument('--service',choices=['both','eoir','ice'],default='both')
    parser.add_argument('--captcha-timeout',type=int,default=600)
    parser.add_argument('--hours',type=float,default=4)
    args=parser.parse_args()
    if not 60<=args.captcha_timeout<=720 or not 0<args.hours<=12:parser.error('Use timeout 60–720 seconds and hours 0–12.')
    if sys.platform!='linux':parser.error('Run this shift on the managed Beacon-4 Linux desktop.')
    import fcntl
    lockpath=Path.home()/'case-tracker-operator'/'staff-shift.lock'
    fd=os.open(str(lockpath),os.O_CREAT|os.O_RDWR,0o600)
    processes=[]
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('A staff shift is already running. Use its four existing windows.')
        multiprocessing.set_start_method('spawn',force=True)
        for index in range(1,args.workers+1):
            proc=multiprocessing.Process(target=worker,args=(index,args.service,args.captcha_timeout,args.hours))
            proc.start();processes.append(proc)
        def stop(*_):
            for proc in processes:
                if proc.is_alive():os.kill(proc.pid,signal.SIGINT)
        signal.signal(signal.SIGTERM,stop)
        try:
            for proc in processes:proc.join()
        except KeyboardInterrupt:
            stop()
            for proc in processes:proc.join(10)
        finally:
            for proc in processes:
                if proc.is_alive():proc.terminate();proc.join(5)
    finally:os.close(fd)


if __name__=='__main__':main()
