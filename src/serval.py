#! /usr/bin/env python
__author__ = 'Mathias Zechmeister'
__version__ = '2017-10-20'

description = '''
SERVAL - SpEctrum Radial Velocity AnaLyser (%s)
     by %s
''' % (__version__, __author__)

import argparse
import copy
import ctypes
from ctypes import c_void_p, c_double, c_int
import csv
# from datetime import datetime imported with read_spec!
import glob
import os
import resource
import stat as os_stat
import sys
import time

import numpy as np
from numpy import std,arange,zeros,where, polynomial,setdiff1d,polyfit,array, newaxis,average
from scipy import interpolate,optimize
from scipy.optimize import curve_fit

from gplot import *
from pause import pause, stop
from wstat import wstd, wmean, wrms, rms, mlrms, iqr, wsem, nanwsem, nanwstd, naniqr, quantile
from golay import *
from read_spec import *   # flag, sflag, def_wlog
from calcspec import *
from targ import Targ
import cubicSpline
import cspline as spl
import masktools
import phoenix_as_RVmodel
from chi2map import Chi2Map


if 'gplot_set' in locals():
   raise ImportError('Please update new gplot.py.')

if tuple(map(int,np.__version__.split('.'))) > (1,6,1):
   np.seterr(invalid='ignore', divide='ignore') # suppression warnings when comparing with nan.

def lam2wave(l, wlog=def_wlog):
   return np.log(l) if wlog else l

def wave2lam(w, wlog=def_wlog):
   return np.exp(w) if wlog else w

servalsrc = os.path.dirname(os.path.realpath(__file__)) + os.sep
servaldir = os.path.dirname(os.path.realpath(servalsrc)) + os.sep
servallib = servaldir + 'lib' + os.sep
os.environ['ASTRO_DATA'] = servalsrc

ptr = np.ctypeslib.ndpointer
_pKolynomial0 = ctypes.CDLL(servalsrc+'polyregression.so')
_pKolynomial0.polyfit.restype = c_double
_pKolynomial = np.ctypeslib.load_library(servalsrc+'polyregression', '.')
_pKolynomial.polyfit.restype = c_double
_pKolynomial.polyfit.argtypes = [
   ptr(dtype=np.float),  # x2
   ptr(dtype=np.float),  # y2
   ptr(dtype=np.float),  # e_y2
   ptr(dtype=np.float),  # fmod
   #ptr(dtype=np.bool),  # ind
   c_double,             # ind
   c_int, c_double, c_int,  # n, wcen, deg
   ptr(dtype=np.float),  # p
   ptr(dtype=np.float),  # lhs
   ptr(dtype=np.float)   # pstat
]
_pKolynomial.interpol1D.argtypes = [
   ptr(dtype=np.float),  # xn
   ptr(dtype=np.float),  # yn
   ptr(dtype=np.float),  # x
   ptr(dtype=np.float),  # y
   c_int, c_int          # nn, n
]

c = 299792.4580   # [km/s] speed of light

def nans(*args, **kwargs):
   return np.nan * np.empty(*args, **kwargs)

# default values
review = 0       # review template
postiter = 3     # number of iterations for post rvs (postclip=3)
debug = 0
sp, fmod = None, None    # @getHalpha
apar = zeros(3)      # parabola parameters
astat = zeros(3*2-1)
alhs = zeros((3, 3))


def Using(point, verb=False):
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if verb: print '%s: usertime=%s systime=%s mem=%s mb' % (point,usage[0],usage[1],
                (usage[2]*resource.getpagesize())/1000000.0 )
    return (usage[2]*resource.getpagesize())/1000000.0


class Logger(object):

   def __init__(self):
      self.terminal = sys.stdout
      self.logfile = None # open(logfilename, "a")
      self.logbuf = ''

   def flush(self):
      # dummy for function astropy iers download; progress bar will not be shown
      pass 

   def write(self, message):   # fork the output to stdout and file
      self.terminal.write(message)
      if self.logfile:
         self.logfile.write(message)
      else:
         self.logbuf += message

   def logname(self, logfilename):
       self.logfile = open(logfilename, 'a')
       self.logfile.write(self.logbuf)
       print 'logging to', logfilename

def minsec(t): return '%um%.3fs' % divmod(t, 60)   # format time


class interp:
   """interpolation similar to interpolate.interp1d but faster
   array must be sorted; 1D arrays only !!!
   """
   def __init__(self, x, y) :
      self.x = 1 * x # we like real arrays
      self.y = 1 * y
   def __call__(self, xx):
      yy = 0 * xx
      _pKolynomial.interpol1D(self.x, self.y, xx, yy, self.x.size, xx.size)
      return yy


def analyse_rv(obj, postiter=1, fibsuf='', oidx=None, safemode=False, pdf=False):
   """
   """
   print obj+'/'+obj+'.rvc'+fibsuf+'.dat'
   allrv = np.genfromtxt(obj+'/'+obj+'.rvo'+fibsuf+'.dat')
   allerr = np.genfromtxt(obj+'/'+obj+'.rvo'+fibsuf+'.daterr')
   sbjd = np.genfromtxt(obj+'/'+obj+'.rvo'+fibsuf+'.dat', dtype=('|S33'), usecols=[0]) # as string
   snr = np.genfromtxt(obj+'/'+obj+'.snr'+fibsuf+'.dat')

   if np.size(allrv) == 1:
      return   # just one line, e.g. drift

   bjd, RVc_old, e_RVc_old, RVd, e_RVd, RV_old, e_RV_old, BRV, RVsa = np.genfromtxt(obj+'/'+obj+'.rvc'+fibsuf+'.dat', dtype=None).T

   orders, = np.where(np.sum(allerr[:,5:]>0, 0))   # orders with all zero error values
   if oidx is not None:
      omiss = set(oidx) - set(orders)
      if omiss: pause('WARNING: orders', omiss,'missing')
      else: orders = np.array(sorted(set(orders) & set(oidx)))

   omap = orders[:,newaxis]
   rv, e_rv = allrv[:,5+orders], allerr[:,5+orders]

   # ind, = where(np.isfinite(e_rv[i])) # do not use the failed and last order
   RV, e_RV = nanwsem(rv, e=e_rv, axis=1)
   RVc = RV - np.nan_to_num(RVd) - np.nan_to_num(RVsa)
   e_RVc = np.sqrt(e_RV**2 + np.nan_to_num(e_RVd)**2)

   # post RVs (re-weightening)
   ok = e_rv > 0
   ordmean = average(rv*0, axis=0)
   gplot.key('tit "'+obj+'"')
   for i in range(1+postiter): # centering, first loop to init, other to clip
      RVp, e_RVp = nanwsem(rv-ordmean, e=e_rv*ok, axis=1)
      orddisp = rv - ordmean - RVp[:,newaxis]
      ordstd, d_ordmean = wstd(orddisp, e_rv*ok, axis=0)
      ordmean += d_ordmean                # update ordmean
      ok &= np.abs(orddisp-d_ordmean) <= 3*ordstd  # clip and update mask
      if np.isnan(RVp.sum()) or np.isnan(e_RVp.sum()):
         print 'WARNING: nan in post RVs. Maybe to few measurements. Re-setting to originals.\n'
         RVp, e_RVp = RV, e_RV
         break
      else:
         gplot(orddisp.T, ' matrix us (%s-1):3 t ""' % "".join(['$1==%s?%s:' % io for io in enumerate(orders)]))
         ogplot(orders, ordstd, ' w lp lt 3 t "", "" us 1:(-$2) w lp t ""')
      if 0: pause(i)
   rvp = rv - ordmean
   pdf=0
   if pdf:
      gplot.term('pdfcairo; set out "%s.pdf"'% obj)
   if 1: # chromatic slope
      gplot.xlabel('"BJD - 2 450 000"').ylabel('"chromatic index [m/s/Np]"')
      gplot('"'+obj+'/'+obj+'.srv'+fibsuf+'.dat" us ($1-2450000):4:5 w e pt 7')
      if not safemode: pause('chromatic slope')
      gplot.xlabel('"RV [m/s]"').ylabel('"chromatic index [m/s/Np]"')
      gplot('"" us 2:4:3:5:($1-2450000)  w xyerr pt 7 palette')
      if not safemode: pause('correlation RV - chromatic slope')

   snro = snr[:,2+orders]
   print "total SNR:", np.sum(snro**2)**0.5
   if 1: # plot SNR
      gplot.reset().xlabel('"Order"; set ylabel "SNR"; set ytics nomirr; set y2label "total SNR"; set y2tics; set yrange[0:]; set y2range[0:]')
      gplot(snro, 'matrix us ($2+%i):3' % np.min(orders), flush='')
      ogplot(np.sum(snro**2,axis=1)**0.5,' us (%i):1 axis x1y2 t "total SNR"'% (np.min(orders)+len(orders)))
      if not safemode: pause()
      gplot.reset()

   # store post processed rvs
   RVpc = RVp - np.nan_to_num(RVd) - np.nan_to_num(RVsa)
   e_RVpc = np.sqrt(e_RVp**2 + np.nan_to_num(e_RVd)**2)
   #unit_rvp = [open(obj+'/'+obj+'.post'+fibsuf+'.dat', 'w'), open(obj+'/'+obj+'.post.badrv'+fibsuf+'.dat', 'w')]
   np.savetxt(obj+'/'+obj+'.post'+fibsuf+'.dat', zip(sbjd, RVpc, e_RVpc, RVp, e_RVp, RVd, e_RVd, BRV, RVsa), fmt='%s')

   print 'Statistic on dispersion in RV time series for', obj
   print '        %10s %10s %10s %10s %10s'% ('mlrms [m/s]', 'std [m/s]', 'wstd [m/s]', 'iqr [m/s]', 'wiqr [m/s]')
   print 'RVmed: %10.4f' % std(allrv[:,3])
   print 'RV:   '+' %10.4f'*5 % (mlrms(RV,e_RV)[0], std(RV), wstd(RV,e_RV)[0], iqr(RV,sigma=True), iqr(RV, w=1/e_RV**2, sigma=True))
   print 'RVp:  '+' %10.4f'*5 % (mlrms(RVp,e_RVp)[0], std(RVp), wstd(RVp,e_RVp)[0], iqr(RVp,sigma=True), iqr(RVp, w=1/e_RVp**2, sigma=True))
   print 'RVc:  '+' %10.4f'*5 % (mlrms(RVc,e_RVc)[0], std(RVc), wstd(RVc,e_RVc)[0], iqr(RVc,sigma=True), iqr(RVc, w=1/e_RVc**2, sigma=True))
   print 'RVpc: '+' %10.4f'*5 % (mlrms(RVpc,e_RVpc)[0], nanwstd(RVpc), nanwstd(RVpc,e=e_RVpc), naniqr(RVpc,sigma=True), iqr(RVpc, w=1/e_RVpc**2, sigma=True))
   print 'median internal precision', np.median(e_RV)
   print  'Time span [d]: ', bjd.max()-bjd.min()

   gplot.reset().xlabel('"BJD - 2 450 000"; set ylabel "RV [m/s]"')
   gplot('"'+obj+'/'+obj+'.rvc'+fibsuf+'.dat" us ($1-2450000):2:3 w e pt 7 t "rvc %s"'%obj)
   if not safemode: pause('rvc')

   gplot(bjd, allrv[:,3],' us ($1-2450000):2 t "RVmedian" lt 4', flush='')
   ogplot(bjd, RV, e_RV, ' us ($1-2450000):2:3 w e t "RV"', flush='')
   ogplot(bjd, RVp, e_RVp, ' us ($1-2450000):2:3 w e t "RVp"', flush='')
   ogplot(bjd, RVc, e_RVc, ' us ($1-2450000):2:3 w e pt 7 lt 1 t "RVc"', flush='')
   ogplot(bjd, RVpc, e_RVpc,' us ($1-2450000):2:3 w e pt 6 lt 7 t "RVpc"')
   if not safemode: pause('rv')

   if 0:  # error comparison
      gplot_set('set xlabel "Order"')
      gplot(orders, ordstd, ' t "external error"')
      ogplot(orders, np.mean(e_rv, axis=0), ' t "internal error"')
      if not safemode: pause('ord disp')
   if 1:  # Order dispersion
      gplot.reset().xlabel('"Order"')
      #gplot('"'+filename,'" matrix every ::%i::%i us ($1-5):3' % (omin+5,omax+5))
      #ogplot(allrv[:,5:71],' matrix every ::%i us 1:3' %omin)
      # create ord, rv,e_rv, bb
      #ore = [(o+omin,orddisp[n,o],e_rv[n,o],~ok[n,o]) for n,x in enumerate(orddisp[:,0]) for o,x in enumerate(orddisp[0,:])]
      #ore = [(o,)+x for row in zip(orddisp,e_rv,~ok) for o,x in enumerate(zip(*row),omin)]
      ore = [np.tile(orders,orddisp.shape).ravel(), orddisp.ravel(), e_rv.ravel(), ~ok.ravel()]
      gplot(*ore + ['us 1:2:($3/30) w xe'], flush='')
      if not ok.all(): ogplot('"" us 1:2:($3/30/$4) w xe', flush='') # mark 3-sigma outliners
      #gplot(orddisp,' matrix  us ($1+%i):3' % omin, flush='')
      #gplot('"'+filename,'" matrix every ::'+str(omin+5)+' us ($1-5):3')
      #ogplot(ordmean,' us ($0+%i):1 w lp lt 3 pt 3  t "ord mean"' %omin, flush='')
      ogplot(orders, ordstd,' w lp lt 3 pt 3  t "1 sigma"', flush='')
      ogplot('"" us 1:(-$2) w lp lt 3 pt 3  t ""', flush='')
      ogplot('"" us 1:($2*3) w lp lt 4 pt 3  t "3 sigma"', flush='')
      ogplot('"" us 1:(-$2*3) w lp lt 4 pt 3  t ""', flush='')
      ogplot('"" us ($1+0.25):(0):(sprintf("%.2f",$2)) w labels rotate t""', flush='')
      ogplot(*ore+[ 'us 1:2:($3)  w point pal pt 6'])
      if not safemode: pause('ord scatter')
   print 'mean ord std:',np.mean(ordstd), ', median ord std:',np.median(ordstd)

   if pdf:
      gplot.out()

   return allrv

def nomask(x):   # dummy function, tellurics are not masked
   return 0. * x


def lineindex(l, r1, r2):
   if np.isnan(r1[0]) or np.isnan(r2[0]):
      return np.nan, np.nan
   s = l[0] / (r1[0]+r2[0]) * 2
   # error propagation:
   e = s * np.sqrt((l[1]/l[0])**2 + (r1[1]**2+r2[1]**2)/(r1[0]+r2[0])**2)
   return s, e

def getHalpha(v, typ='Halpha', inst='HARPS', rel=False, plot=False):
   """
   v [km/s]
   sp,fmod as global variables !
   deblazed sp should be used !
   """
   wcen, dv1, dv2 = {
         'Halpha': (6562.808, -15.5, 15.5),   # Kuerster et al. (2003, A&A, 403, 1077)
         'Halpha': (6562.808, -80, 80),   # Kuerster et al. (2003, A&A, 403, 1077)
         'Halpha': (6562.808, -40., 40.),
         'Haleft': (6562.808, -300., -100.),
         'Harigh': (6562.808, 100, 300),
         'Haleft': (6562.808, -500., -300.),
         'Harigh': (6562.808, 300, 500),
         'CaI':    (6572.795, -15.5, 15.5),   # Kuerster et al. (2003, A&A, 403, 1077)
         'CaH':    (3968.470, -1.09/2./3968.470*c, 1.09/2./3968.470*c), # Lovis et al. (2011, arXiv1107.5325)
         'CaK':    (3933.664, -1.09/2./3933.664*c, 1.09/2./3933.664*c), # Lovis et al.
         'CaIRT1': (8498.02, -15., 15.),      # NIST + my definition
         'CaIRT1a': (8492, -40, 40),          # My definition
         'CaIRT1b': (8504, -40, 40),          # My definition
         'CaIRT2': (8542.09, -15., 15.),      # NIST + my definition
         'CaIRT2a': (8542.09, -300, -200),    # My definition
         'CaIRT2b': (8542.09, 250, 350),      # My definition, +50 due telluric
         'CaIRT3': (8662.14, -15., 15.),      # NIST + my definition
         'CaIRT3a': (8662.14, -300, -200),    # NIST + my definition
         'CaIRT3b': (8662.14, 200, 300),      # NIST + my definition
         'NaD1':   (5889.950943, -15., 15.),  # NIST + my definition
         'NaD2':   (5895.924237, -15., 15.),  # NIST + my definition
         'NaDref1': (5885, -40, 40),          # My definition
         'NaDref2': ((5889.950+5895.924)/2, -40, 40),   # My definition
         'NaDref3': (5905, -40, 40)           # My definition
   }[typ]
   wcen = lam2wave(airtovac(wcen))
   o = None
   if inst == 'HARPS':
      if typ in ['Halpha', 'Haleft', 'Harigh', 'CaI']: o = 67
      if typ in ['CaK']: o = 5
      if typ in ['CaH']: o = 7
   elif inst == 'CARM_VIS':
      if typ in ['Halpha', 'Haleft', 'Harigh', 'CaI']: o = 25
      if 'CaIRT1' in typ: o = 46
      if 'CaIRT2' in typ: o = 46
      if 'CaIRT3' in typ: o = 47
      if 'NaD' in typ: o = 14

   if o is None: return np.nan, np.nan

   ind = o, (wcen+(v-sp.berv+dv1)/c < sp.w[o]) & (sp.w[o] < wcen+(v-sp.berv+dv2)/c)

   if rel: # index relative to template
      # relative index is not a good idea for variable lines
      if np.isnan(fmod[ind]).all(): return np.nan, np.nan
      if np.isnan(fmod[ind]).any(): pause()
      mod = fmod[ind]
      res = sp.f[ind] - mod
      I = np.mean(res/fmod[ind])
      e_I = rms(np.sqrt(sp.f[ind])/fmod[ind]) / np.sqrt(np.sum(ind[1])) # only for photon noise
   else:   # mean absolute flux ("box fitting" .... & ref reg =>  absolute index EW width)
      I = np.mean(sp.f[ind])
      e_I = 1. / ind[1].sum() * np.sqrt(np.sum(sp.e[ind]**2))
      mod = I + 0*sp.f[ind]

   if plot==True or typ in plot:
      print typ, I, e_I
      gplot(sp.w[o], fmod[o], sp.f[o],'us 1:2 w l lt 3 t "template", "" us 1:3 lt 1 t "obs"')
      ogplot(sp.w[ind], sp.f[ind], mod, 'lt 1 pt 7 t"'+typ+'", "" us 1:3 w l t "model", "" us 1:($2-$3) t "residuals"')
      pause()

   return I, e_I


def polyreg(x2, y2, e_y2, v, deg=1, retmod=True):   # polynomial regression
   """Returns polynomial coefficients and goodness of fit."""
   fmod = calcspec(x2, v, 1.)  # get the shifted template
   if 0: # python version
      ind = fmod>0.01     # avoid zero flux, negative flux and overflow
      p,stat = polynomial.polyfit(x2[ind]-calcspec.wcen, y2[ind]/fmod[ind], deg-1, w=fmod[ind]/e_y2[ind], full=True)
      SSR = stat[0][0]
   else: # pure c version
      pstat = np.empty(deg*2-1)
      p = np.empty(deg)
      lhs = np.empty((deg, deg))
      # _pKolynomial.polyfit(x2, y2, e_y2, fmod, ind, ind.size,globvar.wcen, rhs.size, rhs, lhs, pstat, chi)
      ind = 0.0001
      # no check for zero division inside _pKolynomial.polyfit!
      #pause()
      SSR = _pKolynomial.polyfit(x2, y2, e_y2, fmod, ind, x2.size, calcspec.wcen, deg, p, lhs, pstat)
      if SSR < 0:
         ii, = np.where((e_y2<=0) & (fmod>0.01))
         print 'WARNING: Matrix is not positive definite.', 'Zero or negative yerr values for ', ii.size, 'at', ii
         p = 0*p
         pause(0)

      if 0: #abs(v)>200./1000.:
         gplot(x2,y2,calcspec(x2, v, *p), ' w lp,"" us 1:3 w l, "" us 1:($2-$3) t "res [v=%f,SSR=%f]"'%(v, SSR))
         pause('v, SSR: ',v,SSR)
   if retmod: # return the model
      return p, SSR, calcspec(x2, v, *p, fmod=fmod)
   return p, SSR

def gauss(x, a0, a1, a2, a3):
   z = (x-a0) / a2
   y = a1 * np.exp(-z**2 / 2) + a3 #+ a4 * x + a5 * x**2
   return y

def optidrift(ft, df, f2, e2=None):
   """
   Scale derivative to the residuals.

   Model:
      f(v) = A*f - A*df/dv * v/c

   """
   # pre-normalise
   #A = np.dot(ft, f2) / np.dot(ft, ft)   # unweighted (more robust against bad error estimate)
   A = np.dot(1/e2**2*ft, f2) / np.dot(1/e2**2*ft, ft)
   fmod = A * ft
   v = -c * np.dot(1/e2**2*df, f2-fmod) / np.dot(1/e2**2*df, df) / A #**2
   if 0:
      # show RV contribution of each pixel!
      print 'median', np.median(-(f2-fmod)/df*c/A*1000), ' v', v*1000
      gplot(-(f2-fmod)/df*c/A*1000, e2/df*c/A*1000, 'us 0:1:2 w e, %s' % (v*1000))
      pause()
   e_v = c / np.sqrt(np.dot(1/e2**2*df, df)) / A
   fmod = fmod - A*df*v/c
   #gplot(f2, ',', ft*A, ',', f2-fmod,e2, 'us 0:1:2 w e')
   #gplot(f2, ',', ft*A, ',', f2-A*ft,e2, 'us 0:1:2 w e')
   #gplot(f2, ft*A, A*ft-A*df/c*v, A*ft-A*df/c*0.1,' us 0:1, "" us 0:2, "" us 0:3, "" us 0:4, "" us 0:($1-$3) w lp,  "" us 0:($1-$4) w lp lt 7')
   return  type('par',(),{'params': np.append(v,A), 'perror': np.array([e_v,1.0])}), fmod

def CCF(wt, ft, x2, y2, va, vb, e_y2=None, keep=None, plot=False, ccfmode='trapeze'):
   # CCF is on the same level as the least square routine fit_spec
   if keep is None: keep = np.arange(len(x2))
   if e_y2 is None: e_y2 = np.sqrt(y2)
   vgrid = np.arange(va, vb, v_step)

   # CCF is a data compression/smoothing/binning
   SSR = np.arange(vgrid.size, dtype=np.float64)

   def model_box(x, v):
       idx = np.searchsorted(wt+v/c, x)
       return ft[idx] > 0

   for i in SSR:
      if ccfmode == 'box':
         # real boxes (zero order interpolation)
         #idx = np.searchsorted(wt+vgrid[i]/c, x2[keep])
         #SSR[i] = np.dot(y2[keep], ft[idx]>0) / np.sum(ft[idx]>0)
         y2mod = model_box(x2[keep], vgrid[i])
         SSR[i] = np.dot(y2[keep], y2mod) / np.sum(y2mod)
         # gplot(np.exp(x2), y2, ft[idx]*y2.max(),'w lp, "" us 1:3 w l lt 3')
      if ccfmode == 'trapeze':
         stop()
         # gplot(np.exp(x2), y2,'w lp,', np.exp(x2[keep]),ft[idx]*5010, 'w l lt 3')

   # Peak analysis with gaussian fit
   # The Gaussian is a template for the second level product CCF
   try:
      params, covariance = curve_fit(gauss, vgrid, SSR, p0=(0., SSR.max()-SSR.min(), 2.5, SSR.min()))
      perror = np.diag(covariance) if np.isfinite(covariance).min() else 0*params

      SSRmod = gauss(vgrid, *params)
      SSRmin = gauss(params[0], *params)
   except:
      params = [0,0,0,0]
      perror = [0,0,0,0]
      SSRmod = 0
      SSRmin = 0

   if 0:
      # old CCF_binless version
      # uses just the mask which has a fixed window size
      idx = np.searchsorted(wt, x2[keep]) #v=0
      ind = (ft[idx]>0) & (ft[idx-1]>0)
      iidx = idx[ind] - 1 # previous/lower index
      xv2 = (x2[keep[ind]]-0.5*(wt[iidx+1,0]+wt[iidx])) * c
      yv2 = y2[keep[ind]] / ft[iidx]
      ev2 = e_y2[keep[ind]] / ft[iidx]
   elif ccfmode == 'binless':
      # get the line center from the mask
      # grab the data around the line center with the window size
      # phase fold all windows to velocity space by subtracting the line center
      # assume windows do not overlap
      wsz = 6.0 # [km/s] 3.0 for HARPS, 6.0 for FEROS
      linecen = 0.5 * (wt[1::4]+wt[2::4]) # get the linecenter form the mask
      lineflux = 0.5 * (ft[1::4]+ft[2::4])
      idx = np.searchsorted(linecen, x2[keep]) # find index of nearest largest line
      idx = idx - ((linecen[idx]-x2[keep]) > (x2[keep]-linecen[idx-1])) # index of the nearest (smaller or larger) line
      ind = np.abs(x2[keep]-linecen[idx]) < wsz/c # index for values with the window
      xv2 = (x2[keep[ind]]-linecen[idx[ind]]) * c # phase fold
      yv2 = y2[keep[ind]] / lineflux[idx[ind]]
      ev2 = e_y2[keep[ind]] / lineflux[idx[ind]]

      # normalise window flux with the flux mean in each window
      iidx = idx[ind] - 1
      lidx = iidx - iidx.min()
      nlines = lidx.max() + 1
      linenorm = np.zeros((nlines,2))
      for i,y in zip(lidx, yv2): linenorm[i] += np.array([y,1])
      linenorm = (linenorm[:,0] / linenorm[:,1])[lidx]  # mean = sum / n
      yv2norm = yv2 / linenorm
      #yv2 = yv2norm

   fmod = 0
   if 0 or plot and ccfmode=='binless':
      # try a robust gaussfit via iterative clipping
      iidx = idx[ind] - 1
      fmod = 0 * y2
      try:
         #par2, cov2 = curve_fit(gauss, xv2, yv2, p0=(0,1,2.5,0))
         nclip = 1
         good = np.arange(len(xv2))
         for it in range(nclip+1):
            par2, cov2 = curve_fit(gauss, xv2[good], yv2norm[good], p0=(0,1,2.5,0))
            perr2 = np.diag(cov2)
            SSR2mod = gauss(vgrid, *par2)
            yv2mod = gauss(xv2, *par2)
            normfac = params[1] / par2[1]
            fmod[keep[ind]] = gauss(xv2, *par2)
            # clip
            kap = 3 * np.std(yv2norm[good]-yv2mod[good])
            out = np.abs(yv2norm - yv2mod) > kap
            rml = np.unique(lidx[out])
            good = np.sum(lidx == rml[:,newaxis],0)==0 # ind for lines not reject (good windows)
            if plot:
               gplot(xv2, yv2norm, yv2mod,good, ', "" us 1:3, "" us 1:($2/($4==0)),', vgrid,SSR2mod,SSR2mod-kap,SSR2mod+kap, 'w l lt 7, "" us 1:3 w l lt 1, "" us 1:4 w l lt 1')
            #pause()
      except:
         par2 = [0]
         perr2 = [0,0,0]
         normfac = 0
         SSR2mod = vgrid*0

   if plot&1:
      gplot(vgrid, SSR, SSRmod, 'w lp pt 7 t "CCF", "" us 1:3 w l lt 3 lw 3 t"CCF fit %.2f +/- %.2f m/s"'%(params[0]*1000,perror[0]*1000))
   if plot&1 and ccfmode=='binless':
      # insert a -1 row for broken lines in plot
      gg = []
      for g,diff in zip(np.dstack((xv2, normfac*yv2norm, normfac*ev2, iidx-iidx.min()))[0], np.ediff1d(iidx, to_begin=0)): gg += [g*0-1,g] if diff else [g]
      gg = np.array(gg)

      gplot.palette('model HSV rgbformulae 3,2,2; set bar 0')
      gplot(gg, 'us 1:($2/($4>-1)):4 w lp palette pt 7, "" us 1:($2/($4>-1)):3 w e pt 1 lt 1,', vgrid, normfac*SSR2mod, 'w l lc 3 lw 3 t"%.2f +/- %.2f m/s"'%(par2[0]*1000,perr2[2]*1000))
      #gplot(xv2, normfac*yv2norm, normfac*ev2, iidx-iidx.min(), np.ediff1d(iidx, to_begin=0), 'us 1:2:4 w lp palette pt 7, "" us 1:2:3 w e pt 1 lt 1,', vgrid, normfac*SSR2mod, 'w l lc 0 lw 3 t"%.2f +/- %.2f m/s"'%(par2[0]*1000,perr2[2]*1000))
      ogplot(vgrid, SSR, SSRmod, 'w lp lt 7 pt 7 t "CCF", "" us 1:3 w l lt 7 lw 3 t"CCF fit %.2f +/- %.2f m/s"'%(params[0]*1000,perror[0]*1000))

      pause(params[0]*1000, perror[0]*1000)
      print params[0]*1000, perror[0]*1000

   if plot&1: pause()
   if plot&2:
      idx = np.searchsorted(wt, x2)
      idx0 = np.searchsorted(wt-vgrid[0]/c, x2)
      gplot(np.exp(x2), y2, ft[idx]/10, y2*(ft[idx]>0), y2*(ft[idx0]>0), 'w lp t "x2,y2 input spectrum", "" us 1:3 w l lt 3 t "mask (v=0)", "" us 1:4 lt 3 t "x2,y2*mask", "" us 1:5  lt 4 pt 1 t "x2,y2*mask(v=va)')
      pause()
   stat = {'std': 1, 'snr': 0}
   params[0] *= -1
   return type('par',(),{'params': params, 'perror':perror, 'ssr':SSRmin, 'niter':0}), fmod, (vgrid, SSR, SSRmod), stat

def SSRstat(vgrid, SSR, dk=1, plot='maybe'):
   # analyse peak
   k = SSR[dk:-dk].argmin() + dk   # best point (exclude borders)
   vpeak = vgrid[k-dk:k+dk+1]
   SSRpeak = SSR[k-dk:k+dk+1] - SSR[k]
   # interpolating parabola (direct solution) through the three pixels in the minimum
   a = np.array([0, (SSR[k+dk]-SSR[k-dk])/(2*v_step), (SSR[k+dk]-2*SSR[k]+SSR[k-dk])/(2*v_step**2)])  # interpolating parabola for even grid
   v = (SSR[k+dk]-SSR[k-dk]) / (SSR[k+dk]-2*SSR[k]+SSR[k-dk]) * 0.5 * v_step

   v = vgrid[k] - a[1]/2./a[2]   # parabola minimum
   e_v = np.nan
   if -1 in SSR:
      print 'opti warning: bad ccf.'
   elif a[2] <= 0:
      print 'opti warning: a[2]=%f<=0.' % a[2]
   elif not vgrid[0] <= v <= vgrid[-1]:
      print 'opti warning: v not in [va,vb].'
   else:
      e_v = 1. / a[2]**0.5
   if (plot==1 and np.isnan(e_v)) or plot==2:
      gplot.yrange('[*:%f]'%SSR.max())
      gplot(vgrid, SSR-SSR[k], " w lp, v1="+str(vgrid[k])+", %f+(x-v1)*%f+(x-v1)**2*%f," % tuple(a), [v,v], [0,SSR[1]], 'w l t "%f km/s"'%v)
      ogplot(vpeak, SSRpeak, ' lt 1 pt 6; set yrange [*:*]')
      pause(v)
   return v, e_v, a

def opti(va, vb, x2, y2, e_y2, p=None, vfix=False, plot=False):
   """ vfix to fix v for RV constant stars?
   performs a mini CCF; the grid stepping
   returns best v and errors from parabola curvature
   """
   vgrid = np.arange(va, vb, v_step)
   nk = len(vgrid)

   SSR = np.empty(nk)
   for k in range(nk):
      p, SSR[k] = polyreg(x2, y2, e_y2, vgrid[k], len(p), retmod=False)

   # analyse the CCF peak fitting
   v, e_v, a = SSRstat(vgrid, SSR, plot=(not safemode)*(1+plot))

   if np.isnan(e_v):
      v = vgrid[nk/2]   # actually it should be nan, but may the next clipping loop or plot use vcen
      print " Setting  v=" % v
   if vfix: v = 0.
   p, SSRmin, fmod = polyreg(x2, y2, e_y2, v, len(p))   # final call with v

   if 0 and (np.isnan(e_v) or plot) and not safemode:
      gplot(x2, y2, fmod, ' w lp, "" us 1:3 w lp lt 3')
      pause(v)
   return type('par', (), {'params': np.append(v,p), 'perror': np.array([e_v,1.0]), 'ssr': (vgrid,SSR)}), fmod

def fitspec(wt, ft, tck, w2, f2, e_y=None, v=0, vfix=False, clip=None, nclip=1, keep=None, indmod=np.s_[:], v_step=True, df=None, plot=False, deg=3, chi2map=False):
   """
   Performs the robust least square fit via iterative clipping.

   vfix : boolean, optional
       v is fixed. For RV constant stars or in coadding when only the background polynomial is computed.
   indmod : Index range to be finally calculated-
   clip : Kappa sigma clipping value.
   nclip : Number of clipping iterations (default: 0 if clip else 1).
   df : Derivative for drift measurement.
   v_step - Number of v steps (only background polynomial => v_step = false).

   """
   calcspec.wcen = np.mean(w2)
   calcspec.tck = tck
   if keep is None: keep = np.arange(len(w2))
   if e_y is None: e_y = np.mean(f2)**0.5 + 0*f2   # mean photon noise
   if clip is None: nclip = 0   # number of clip iterations; default 1

   p = np.array([v, 1.] + [0]*deg)   # => [v,1,0,0,0]
   fMod = np.nan * w2
   #fres = 0.*w2     # same size
   for n in range(nclip+1):
      if df is not None:
         '''drift mode: scale derivative to residuals'''
         par, fModkeep = optidrift(ft.take(keep,mode='clip'), df.take(keep,mode='clip'), f2.take(keep,mode='clip'),
                                 e_y.take(keep,mode='clip'))
      elif v_step:
         '''least square mode'''
         par, fModkeep = opti(v+v_lo, v+v_hi, w2.take(keep,mode='clip'), f2.take(keep,mode='clip'),
                              e_y.take(keep,mode='clip'), p[1:], vfix=vfix, plot=plot)
         ssr = par.ssr
      else:
         '''only background polynomial'''
         p, SSR, fModkeep = polyreg(w2.take(keep,mode='clip'), f2.take(keep,mode='clip'), e_y.take(keep,mode='clip'), v, len(p)-1)
         par = type('par',(),{'params': np.append(v,p), 'ssr': SSR})

      p = par.params
      par.niter = n
      if 1:
         # exclude model regions with negative flux
         # can occur e.g. in background in ThAr, or due to tellurics
         ind = fModkeep > 0
         keep = keep[ind]      # ignore the pixels modelled with negative flux
         fModkeep = fModkeep[ind]
         # all might be negative (for low/zero S/N data)
      #fres[keep] = (f2[keep] - fMod[keep]) / fMod[keep]**0.5
      #res_std = rms(fres[keep])     # residual noise / photon noise
      # use error predicted by model
      #fres = (f2.take(keep,mode='clip')-fModkeep) / fModkeep**0.5
      # use external errors
      fres = (f2.take(keep,mode='clip')-fModkeep) / e_y.take(keep,mode='clip')
      res_std = rms(fres)     # residual noise / photon noise
      if n < nclip:
         ind = np.abs(fres) <= clip*res_std
         nreject = len(keep) - np.sum(ind)
         if nreject: keep = keep[ind]   # prepare next clip loop
         # else: break
      if len(keep)<10: # too much rejected? too many negative values?
         print "too much rejected, skipping"
         break
      if 0 and np.abs(par.params[0]*1000)>20:
         if df:
            fMod = ft * p[1]     # compute also at bad pixels
         else:
            fMod = calcspec(w2, *p)     # compute also at bad pixels
         gplot.y2tics().ytics('nomir; set y2range [-5:35];')
         gplot(w2,fMod,' w lp pt 7 ps 0.5 t "fmod"',flush='');
         ogplot(w2[keep],fMod[keep],' w lp pt 7 ps 0.5 t "fmod[keep]"',flush='');
         ogplot(w2,f2,' w lp pt 7 ps 0.5 t "f2"',flush='');
         #ogplot(w2[keep],fres[keep],' w lp pt 7 ps 0.5 lc rgb "red" axis x1y2 t "residuals"',flush='')
         ogplot(w2[keep],fres,' w lp pt 7 ps 0.5 lc rgb "black" axis x1y2, 0 w l lt 2 axis x1y2 t"", '+str(res_std)+' w l lt 1 axis x1y2, '+str(-res_std)+ ' w l lt 1 t "" axis x1y2')
         pause('large RV', par.params[0]*1000)

   stat = {"std": res_std, "snr": np.mean(fModkeep)/np.mean(np.abs(f2.take(keep,mode='clip')-fModkeep))}
   #pause(stat["snr"], wmean(fModkeep)/wrms(f2.take(keep,mode='clip')-fModkeep), np.median(fModkeep)/np.median(np.abs(f2.take(keep,mode='clip')-fModkeep)) )
   if df is not None:
      fMod[indmod] = ft[indmod]*p[1] - df[indmod]*p[1]*p[0]/c  # compute also at bad pixels
   else:
      fMod[indmod] = calcspec(w2[indmod], *p)   # compute also at bad pixels

   if chi2map:
      return par, fMod, keep, stat, ssr
   else:
      return par, fMod, keep, stat



def serval(*argv):

   sys.stdout = Logger()

   global obj, targ, oset, coadd, coset, last, tpl, sa, tplrv, debug, sp, fmod, reana, inst, fib, look, looki, lookt, lookp, lookssr, pmin, pmax, debug, pspllam, kapsig, nclip, atmfile, skyfile, atmwgt, omin, omax, ptmin, ptmax, driftref, deg, targrv, starcat

   if not argv: argv = sys.argv     # python shell start
   else: argv = ['module '] + list(argv)
   if len(argv)>1:
      outdir = obj + '/'
      fibsuf = '_B' if inst=='FEROS' and fib=='B' else ''
   else:
      print "object missing"
      print "Example: "+sys.argv[0]+" gj699  dir_or_inputlist=/home/zechmeister/data/harps/gj699/path/ -restore omin=20 iset='1::2'"
      return 1

   print description

   ### SELECT INSTRUMENTAL FORMAT ###
   # general default values
   pat = '*tar' # default search pattern
   maskfile = servallib + 'telluric_mask_atlas_short.dat'

   # instrument specific default values
   if inst == 'CARM_VIS':
      if fib == '': fib = 'A'
      iomax = 61 # NAXIS2
      # pat = '*pho*_x2d_'+fib+'.fits'
      pat = '*-vis_' + fib + '.fits'
      maskfile = servallib + 'telluric_mask_carm_short.dat'
   elif inst == 'CARM_NIR':
      if fib == '': fib = 'A'
      iomax = 28
      iomax *= 2 # reshaping
      pat = '*-nir_' + fib + '.fits'
      maskfile = servallib + 'telluric_mask_carm_short.dat'
   elif 'HARP' in inst:
      if fib == '': fib = 'A'
      if fib == 'A': iomax = 72
      if fib == 'B': iomax = 71
      if inst=='HARPN': iomax = 68
   elif inst == 'FEROS':
      iomax = 38
      if fib == '': fib = 'A'
      if fib == 'B': maskfile = servallib + 'feros_mask_short.dat'
      ptomin = np.array([1800, 2000, 1800, 2000, 2000, 1600, 1500, 1400, 1100, 1000,
                         1000, 1000, 1000,  900,  800,  800,  800,  600,  500,  500,
                          400,  400,  300,  100,  100,  100,  100,  100,  100,  100,
                          100,  100,  100,  100,  100,  100,  100,  100,  100])
      ptomax = np.array([3100, 4000, 4000, 4000, 4000, 4500, 4600, 4600, 4800, 4800,
                         5000, 5200, 5200, 5300, 5600, 5700, 5800, 6000, 6100, 6300,
                         6500, 6600, 6700, 6800, 7100, 7200, 7400, 7700, 7900, 8100,
                         8400, 8600, 8900, 9200, 9500, 9800,10200,10600,11100])
      pomin = ptomin + 300
      pomax = ptomax - 300
      pmin = pomin.min()
      pmax = pomin.max()
   elif inst == 'FTS':
      iomax = 70
      pmin = 300
      pmax = 50000/5 - 500

   ptmin = pmin - 100   # oversize the template
   ptmax = pmax + 100

   orders = np.arange(iomax)[oset]
   corders = np.arange(iomax)[coset]

   orders = sorted(set(orders) - set(o_excl))
   corders = sorted(set(corders) - set(co_excl))
   omin = min(orders)
   omax = max(orders)
   comin = min(corders)
   comax = max(corders)

   if reana:
      x = analyse_rv(obj, postiter=postiter, fibsuf=fibsuf, oidx=orders)
      exit()

   os.system('mkdir -p '+obj)

   ### SELECT TARGET ###
   targ = type('TARG', (), {'name': targ, 'plx': targplx, 'sa': float('nan')})
   targ.ra, targ.de = targrade
   targ.pmra, targ.pmde = targpm
   use_drsberv = False
   if fib == 'B' or (ccf is not None and 'th_mask' in ccf):
      pass

   if targ.name == 'cal':
      print 'no barycentric correction (calibration)'
   elif targ.ra and targ.de or targ.name:
      targ = Targ(targ.name, targrade, targpm, plx=targplx, rv=targrv, cvs=obj+'/'+obj+'.targ.cvs')
      '''
   elif targ.ra and targ.de:
      targ.ra = tuple(map(float,targ.ra.split(':')))
      targ.de = tuple(map(float,targ.de.split(':')))
   elif targ.name:
      if starcat:
         if os.path.isfile(starcat):    # user file
            starcat = starcat
         elif os.path.isdir(starcat):   # user dir
            starcat = os.path.join(starcat, 'star.cat')
      else:
         if os.path.isfile('star.cat'): # local file
            starcat = 'star.cat'
         elif os.path.isfile(servaldir+'star.cat'): # src file
            starcat = servaldir + 'star.cat'
      print 'Looking for', targ.name, 'in ', starcat + '.'
      for line in open(starcat):
         if line.startswith(targ.name+' '):
            line = line.split()
            targ.ra = tuple(map(float,line[3:6]))  # rammss = (14.,29.,42.94)
            targ.de = tuple(map(float,line[6:9]))  # demmss = (-62.,40.,46.16)
            targ.pmra = float(line[9])             # pma = -3775.75
            targ.pmde = float(line[10])            # pmd = 765.54
            targ.sa = float(line[2])

      if targ.ra is None:
         print targ.name, ' not found in ', starcat + '.'
         usersa = raw_input('Continue with DRSBERVs? Then, please enter a secular acceleration [0.0 m/s/yr]:')
         use_drsberv = True
         if usersa != '':
            targ.sa = float(usersa)
      '''
      print ' using sa=', targ.sa, 'm/s/yr', 'ra=', targ.ra, 'de=', targ.de, 'pmra=', targ.pmra, 'pmde=', targ.pmde
   else:
      print 'using barycentric correction from DRS'
   # if targ.plx is not None: targ.sa = ...

   # choose the interpolation type
   spltype = 3 # 3=> fast version
   spline_cv = {1: interpolate.splrep, 2: cubicSpline.spl_c,  3: cubicSpline.spl_cf }[spltype]
   spline_ev = {1: interpolate.splev,  2: cubicSpline.spl_ev, 3: cubicSpline.spl_evf}[spltype]
   t0 = time.time()

   print dir_or_inputlist
   print 'tpl=%s pmin=%s iset=%s omin=%s omax=%s' % (tpl, pmin, iset, omin, omax)

   ''' SELECT FILES '''
   files = sorted(glob.glob(dir_or_inputlist+os.sep+pat))

   isfifo = os_stat.S_ISFIFO(os.stat(dir_or_inputlist).st_mode)
   if os.path.isfile(dir_or_inputlist) or isfifo:
      if dir_or_inputlist.endswith(('.txt', '.lis')) or isfifo:
         files = []
         with open(dir_or_inputlist) as f:
            print 'getting filenames from file (',dir_or_inputlist,'):'
            for line in f:
               line = line.split()   # remove comments
               if line:
                  if os.path.isfile(line[0]):
                     files += [line[0]]
                     print len(files), files[-1]
                  else:
                     print 'skipping:', line[0]
         if fib == 'B':
           files = [f.replace('_A.fits','_B.fits') for f in files]
           print 'renaming', files
      else:
         # works if dir_or_inputlist is one fits-file
         files = [dir_or_inputlist]

   # handle tar, e2ds, fox
   if 'HARP' in inst and not files:
      files = sorted(glob.glob(dir_or_inputlist+'/*e2ds_'+fib+'.fits'))
      if not files:
         files = sorted(glob.glob(dir_or_inputlist+'/*e2ds_'+fib+'.fits.gz'))
   drs = bool(len(files))
   if 'HARPS' in inst and not drs:  # fox
      files = sorted(glob.glob(dir_or_inputlist+'/*[0-9]_'+fib+'.fits'))
   if 'CARM' in inst and not files:
      files = sorted(glob.glob(dir_or_inputlist+'/*pho*_'+fib+'.fits'))
   if 'FEROS' in inst:
      files += sorted(glob.glob(dir_or_inputlist+'/f*'+('1' if fib=='A' else '2')+'0001.mt'))
   if 'FTS' in inst:
      files = sorted(glob.glob(dir_or_inputlist+'/*ap08.*_ScSm.txt'))
      files = [s for s in files if '20_ap08.1_ScSm.txt' not in s and '20_ap08.2_ScSm.txt' not in s and '001_08_ap08.193_ScSm.txt' not in s ]

   files = np.array(files)[iset]
   nspec = len(files)
   if not nspec:
      print "no spectra found in", dir_or_inputlist, 'or using ', pat, inst
      exit()
   # expand slices to index arrays
   if look: look = np.arange(iomax)[look]
   if lookt: lookt = np.arange(iomax)[lookt]
   if lookp: lookp = np.arange(iomax)[lookp]
   if lookssr: lookssr = np.arange(iomax)[lookssr]

   if outfmt or outchi: os.system('mkdir -p '+obj+'/res')
   with open(outdir+'lastcmd.txt', 'w') as f:
      print >>f, ' '.join(argv)
   with open('cmdhistory.txt', 'a') as f:
      print >>f, ' '.join(argv)

   badfile = file(outdir + obj + '.flagdrs' + fibsuf + '.dat', 'w')
   infofile = file(outdir + obj + '.info' + fibsuf + '.cvs', 'w')
   bervfile = open(outdir + obj + '.brv' + fibsuf + '.dat', 'w')
   prefile = outdir + obj + '.pre' + fibsuf + '.dat'
   rvofile = outdir + obj + '.rvo' + fibsuf + '.dat'
   snrfile = outdir + obj + '.snr' + fibsuf + '.dat'
   chifile = outdir + obj + '.chi' + fibsuf + '.dat'
   halfile = outdir + obj + '.halpha' + fibsuf + '.dat'
   nadfile = outdir + obj + '.nad' + fibsuf + '.dat'
   irtfile = outdir + obj + '.cairt' + fibsuf + '.dat'
   dfwfile = outdir + obj + '.dlw' + fibsuf + '.dat'

   # (echo 0 0 ; awk '{if($2!=x2){print x; print $0}; x=$0; x2=$2;}' telluric_mask_atlas.dat )> telluric_mask_atlas_short.dat
   #################################
   ### Loading and prepare masks ###
   #################################
   mask = None
   tellmask = nomask
   skymsk = nomask

   if fib == 'B':
      if atmfile == 'auto':
         atmfile = None
      if skyfile == 'auto':
         skyfile = None

   if ccf and 'th_mask' not in ccf:
      atmfile = None

   if atmfile:
      if atmfile != 'auto':
         maskfile = atmfile
      if 'mask_ne' in atmfile:
         maskfile = servallib + atmfile

      print 'maskfile', maskfile
      mask = np.genfromtxt(maskfile, dtype=None)

      if 'telluric_mask_atlas_short.dat' in maskfile:
         lcorr = 0.000009  # Guillems mask needs this shift of 2.7 km/s
         mask[:,0] = airtovac(mask[:,0]) * (1-lcorr)
      if 'th_mask' in maskfile: # well, it is not atmosphere, but ...
         mask[:,1] = mask[:,1] == 0  # invert mask; actually the telluric mask should be inverted (so that 1 means flagged and bad)

   if skyfile:
      if skyfile=='auto' and inst=='CARM_NIR':
         skyfile = servallib + 'sky_carm_nir'
         sky = np.genfromtxt(skyfile, dtype=None)
         skymsk = interp(lam2wave(sky[:,0]), sky[:,1])


   msksky = [0] * iomax
   if 1 and inst=='CARM_VIS':
      import pyfits
      msksky = flag.atm * pyfits.getdata(servallib + 'carm_vis_tel_sky.fits')

   if msklist: # convert line list to mask
      mask = masktools.list2mask(msklist, wd=mskwd)
      mask[:,1] = mask[:,1] == 0  # invert mask; actually the telluric mask should be inverted (so that 1 means good)

   if mask is None:
      print 'using telluric mask: NONE'
   else:
      # make the discrete mask to a continuous mask via linear interpolation
      tellmask = interp(lam2wave(mask[:,0]), mask[:,1])
      print 'using telluric mask: ', maskfile

   if 0:
      mask2 = np.genfromtxt('telluric_add.dat', dtype=None)
      # DO YOU NEED THIS: mask2[:,0] = airtovac(mask2[:,0])  ??
      i0 = 0 #where(mask[:,0]<mask2[0][0])[0][-1]
      for ran in mask2:
         while (mask[i0,0]<ran[0]): i0 += 1
         #if mask[i0-1,0]==0:
         mask = np.insert(mask,i0,[ran[0]-0.0000001,0.0],axis=0) # insert
         mask = np.insert(mask,i0+1,[ran[0],1.0],axis=0) # insert
         #while (mask[i0,0]<ran[1]): np.delete(mask,i0,axis=0)
         #if mask[i0-1,0]==0:
         mask = np.insert(mask,i0+2,[ran[1],1.0],axis=0) # insert
         mask = np.insert(mask,i0+3,[ran[1]+0.0000001,0.0],axis=0) # insert

   ################################
   ### READ FITS FILES ############
   ################################
   splist = []
   spi = None
   SN55best = 0.
   print "    # %*s %*s OBJECT    BJD        SN  DRSBERV  DRSdrift flag calmode" % (-len(inst)-6, "inst_mode", -len(os.path.basename(files[0])), "timeid")
   infowriter = csv.writer(infofile, delimiter=';', lineterminator="\n")

   for n,filename in enumerate(files):   # scanning fitsheader
      print '%3i/%i' % (n+1, nspec),
      sp = Spectrum(filename, inst=inst, pfits=2 if 'HARP' in inst else True, drs=drs, fib=fib, targ=targ, verb=True)
      splist.append(sp)
      if use_drsberv:
         sp.bjd, sp.berv = sp.drsbjd, sp.drsberv
      sp.sa = targ.sa / 365.25 * (sp.bjd-splist[0].bjd)
      sp.header = None   # saves memory(?), but needs re-read (?)
      if inst == 'HARPS' and drs: sp.ccf = read_harps_ccf(filename)
      if sp.sn55 < snmin: sp.flag |= sflag.lowSN
      if sp.sn55 > snmax: sp.flag |= sflag.hiSN
      if distmax and sp.ra and sp.de:
         # check distance for mis-pointings
         # yet no proper motion included
         ra = (targ.ra[0] + (targ.ra[1]/60 + targ.ra[2]/3600)* np.sign(targ.ra[0]))*15   # [deg]
         de = targ.de[0] + (targ.de[1]/60 + targ.de[2]/3600)* np.sign(targ.de[0])   # [deg]
         dist = np.sqrt(((sp.ra-ra)*np.cos(np.deg2rad(sp.de)))**2 + (sp.de-de)**2) * 3600
         if dist > distmax: sp.flag |= sflag.dist
      if not sp.flag:
         if SN55best < sp.sn55 < snmax:
            SN55best = sp.sn55
            spi = n
      else:
         print >>badfile, sp.bjd, sp.ccf.rvc, sp.ccf.err_rvc, sp.timeid, sp.flag
      print >>bervfile, sp.bjd, sp.berv, sp.drsbjd, sp.drsberv, sp.drift, sp.timeid, sp.tmmean, sp.exptime
      infowriter.writerow([sp.timeid, sp.bjd, sp.berv, sp.sn55, sp.obj, sp.exptime, sp.ccf.mask, sp.flag, sp.airmass, sp.ra, sp.de])
      #print >>infofile, sp.timeid, sp.bjd, sp.berv, sp.sn55, sp.obj, sp.exptime, sp.ccf.mask, sp.flag

   badfile.close()
   bervfile.close()
   infofile.close()
   sys.stdout.logname(obj+'/log.'+obj)

   t1 = time.time() - t0
   print nspec, "spectra read (%s)\n" % minsec(t1)

   # filter for the good spectra
   check_daytime = True
   spoklist = []
   for sp in splist:
      if sp.flag & (sflag.eggs|sflag.dist|sflag.lowSN|sflag.hiSN|sflag.led|check_daytime*sflag.daytime):
         print 'bad spectra:', sp.timeid, 'sn: %s flag: %s %s' % (sp.sn55, sp.flag, sflag.translate(sp.flag))
      else:
         spoklist += [sp]

   nspecok = len(spoklist)
   if not nspecok:
      print "WARNING: no good spectra"
      if not safemode: pause()   # ???

   rvcmedian = np.median([sp.ccf.rvc for sp in spoklist])
   snrmedian = np.median([sp.sn55 for sp in spoklist])
   with open(outdir+obj+'.drs.dat', 'w') as myunit:
      for sp in spoklist:
         print >>myunit, sp.bjd, sp.ccf.rvc*1000., sp.ccf.err_rvc*1000., sp.ccf.fwhm, sp.ccf.bis, sp.ccf.contrast, sp.timeid
         # print >>myunit, sp.bjd, sp.ccf.rvc-rvcmedian, sp.ccf.err_rvc, sp.ccf.fwhm, sp.ccf.bis, sp.ccf.contrast, sp.timeid

   if spi is None:
      print 'No highest S/N found; selecting first file as reference'
      spi = 0

   if last:
      tpl = outdir + 'template' + fibsuf + '.fits'
   elif tpl is None:
      tpl = spi   # choose highest S/N spectrum

   if isinstance(tpl, int):
      spi = tpl
   spt = files[spi]   # splist[tpl]

   # In any case we want to read the highest S/N spectrum (e.g. @wfix).
   # Use pyfits to get the full header
   spt = Spectrum(spt, inst=inst, pfits=True, orders=np.s_[:], drs=drs, fib=fib, targ=targ)
   # Estimate a Q for each order to identify fast rotators
   # Estimation similar as in Bouchy01
   # yet tellurics are not masked
   Wi = (spt.f[:,2:]-spt.f[:,:-2]) / (spt.w[:,2:]-spt.w[:,:-2]) / spt.e[:,1:-1]   # Eq.(8)
   dv = c / np.sqrt(np.nansum(Wi**2, axis=1))   # theoretical RV precision Eq.(10)
   sn = np.sqrt(np.nansum((spt.f[:,1:-1] / spt.e[:,1:-1])**2, axis=1))   # total SNR over order, corresponds to sqrt(Ne) in Bouchy
   Q = c / dv / sn
   #Q = np.sqrt(np.nansum(Wi**2, axis=1)) / np.sqrt(np.nansum((spt.f[:,1:-1] / spt.e[:,1:-1])**2, axis=1) # a robust variant
   #gplot(Q)

   print 'median SN:', snrmedian
   print 'template:', spt.timeid, 'SN55', spt.sn55, '#', spi, ' <e_rv>=%0.2fm/s, <Q>=%s' % (np.median(dv)*1000, np.median(Q))

   if nspec>40 and coadd=='post':
      print 'n>40 Forcing flying coadd'
      coadd = 'fly'

   ################################
   ### create high S_N template ###
   ################################
   print ''
   ntpix = ptmax - ptmin
   pixx = arange(ntpix)
   pixxx = arange((ntpix-1)*4+1) / 4.
   nord = len(spt.w[:,0])
   osize = len(pixxx)
   ww = np.ones((nord,osize))
   ff = np.zeros((nord,osize))
   ee = np.zeros((nord,osize))
   bb = np.zeros((nord,osize), dtype=int)
   nn = np.zeros((nord,osize))

   # for post 3
   nk = int(osize / (8 if inst=='FEROS' else 4) * ofac)
   wk = nans((nord,nk))
   fk = nans((nord,nk))
   ek = nans((nord,nk))
   bk = np.zeros((nord,nk))
   #kk = zeros((nord,5,osize))
   kk = [[0]]*nord

   norm = np.ones((nord))
   if inst == 'FEROS':
      ww = [0] * nord
      ff = [0] * nord
      ee = [0] * nord
      bb = [0] * nord
      nn = [0] * nord
      ntopix = ptomax - ptomin

   rv, e_rv = nans((2, nspec, nord))
   RV, e_RV = nans((2, nspec))

   if coadd == "post":
      mod = zeros((nspecok,nord,osize))
      emod = zeros((nspecok,nord,osize))
      bmod = zeros((nspecok,nord,osize), dtype=int)
      coeffs = zeros((nspecok,nord,5))

   ordwrappedtemplate = True   # echelle or continuous spectrum

   # skip preRVs and template creation for ccf, drift and restoring template
   if ccf:
      ccfmask = np.loadtxt(servallib + ccf)
   elif driftref:
      print driftref
      spt = Spectrum(driftref, inst=inst, pfits=True, orders=np.s_[:], drs=drs, fib=fib, targ=targ)
      ww, ff = spt.w, spt.f
   elif isinstance(tpl, str):
      print "restoring template: ", tpl
      try:
         if 'phoe' in tpl:
            ww, ff = phoenix_as_RVmodel.readphoenix(servallib + 'lte03900-5.00-0.0_carm.fits', wmin=np.exp(np.nanmin(spt.w)), wmax=np.exp(np.nanmax(spt.w)))
            ww = lam2wave(ww)
            kk = spline_cv(ww, ff)
            ordwrappedtemplate = False
            ww = [ww] * (omax+1)
            ff = [ff] * (omax+1)
            kk = [kk] * (omax+1)
         elif tpl.endswith('template.fits') or os.path.isdir(tpl):
            # read a spectrum stored order wise
            ww, ff, head = read_template(tpl+(os.sep+'template.fits' if os.path.isdir(tpl) else ''))
            print 'HIERARCH SERVAL COADD NUM:', head['HIERARCH SERVAL COADD NUM']
            if not 'PHOENIX-ACES-AGSS-COND' in tpl:
               if omin<head['HIERARCH SERVAL COADD COMIN']: pause('omin to small')
               if omax>head['HIERARCH SERVAL COADD COMAX']: pause('omax to large')
         else:
            spt = Spectrum(tpl, inst=inst, pfits=True, orders=np.s_[:], drs=drs, fib=fib, targ=targ)
            ww, ff = barshift(spt.w,spt.berv), spt.f
      except:
         print 'ERROR: could not read template:', tpl
         exit()

      if inst == 'FEROS':
         www = [0] * len(ww)
         fff = [0] * len(ww)
         for o in range(len(ww)):
            ind = ww[o] > 0 # remove padded zeros
            if ind.any():
               www[o] = ww[o][ind]
               fff[o] = ff[o][ind]
         ww = www
         ff = fff
   else:
      '''set up a spline interpolated, oversampled template from spt'''
      tpl = outdir + 'template_' +coadd + fibsuf + '.fits'
      for o in sorted(set(orders) | set(corders)):
         if inst == 'FEROS':
            ptmin = ptomin[o]
            ptmax = ptomax[o]
            pixx = arange(ntopix[o])
            pixxx = arange((ntopix[o]-1)*4+1)/4.
            osize = len(pixxx)
            bb[o] = np.zeros(osize,dtype=int)
         pixx, = where((np.isfinite(spt.w) & np.isfinite(spt.f) & np.isfinite(spt.e))[o,ptmin:ptmax])
         idx = pixx + ptmin
         kktmp = spline_cv(pixx, barshift(spt.w[o,idx],spt.berv))
         ww[o] = spline_ev(pixxx, kktmp)
         kk[o] = spline_cv(barshift(spt.w[o,idx],spt.berv), spt.f[o,idx])
         ff[o] = spline_ev(ww[o], kk[o])
         # interpolate errors not good but needed for weighting!!!
         kktmp = spline_cv(barshift(spt.w[o,idx],spt.berv), spt.e[o,idx])
         ee[o] = spline_ev(ww[o], kktmp) # can give negative errors
         ind = spt.bpmap[o,idx] == 0  # let out zero errors, interpolate over
         ind[0] = True
         ind[-1] = True
         #ee[o,idx[ind]]= interpolate.interp1d(barshift(spt.w[o,idx][ind],spt.berv), spt.e[o,idx][ind])(ww[o,idx[ind]]) # linear
         ee[o] = interpolate.interp1d(barshift(spt.w[o,idx][ind],spt.berv), spt.e[o,idx][ind], fill_value="extrapolate")(ww[o]) # linear, sometimes extrapolate at border might be required, if barshift(spt.w[o,idx][ind][-1],spt.berv) == ww[o][-5]

         if 0 or o==-50:
            gplot(ww[o],ff[o], ',', barshift(spt.w[o,ptmin:ptmax],spt.berv), spt.f[o,ptmin:ptmax])
            pause(o)
         bb[o][tellmask(barshift(ww[o],-spt.berv))>0.01] |= flag.atm   # mask as before berv correction
         bb[o][skymsk(barshift(ww[o],-spt.berv))>0.01] |= flag.sky   # mask as before berv correction
         bb[o][ee[o]<=0] |= flag.nan   # mask as before berv correction
         # negative interpolated are occured once around a 0.0 value
         if inst == 'FEROS':
            # works with list so we do it here
            bb[o][ff[o]<0] |= flag.neg

      if inst != 'FEROS':
         bb[ff<0] |= flag.neg
      #ind = (bb&flag.neg)==0
      #gplot(barshift(spt.w[o,ptmin:ptmax],spt.berv),spt.f[o,ptmin:ptmax])
      #ogplot(ww[o],ff[o]); pause()
      to = time.time()

      if skippre:   # restore the pre RVs
         if os.path.isfile(prefile):
            bjd, RV, e_RV = np.genfromtxt(prefile, dtype=None, unpack=True)
         else:
            pause('pre RV file', prefile, 'does not exist')
         RV = -RV   # swapped template
      else:  # measure pre-RVs and for fly mode improve template by coadding
       if review>1: gplot('0')
       myunit = file(prefile, 'w')

       for i,sp in enumerate(spoklist[tset]):
        if sp.flag:
           print "\nNot using flagged spectrum:", i, sp.filename, 'flag:', sp.flag
        else:
         if inst!='FEROS': sp = copy.deepcopy(sp)  # for FEROS no single orders reading, store everything
         sp.read_data(pfits=2)   # use the faster version
         mem = Using('pre')
         if mem>3000: pause('MEM>', 3000,' MB')
         for o in orders:
            sp.bpmap[o][tellmask(sp.w[o])>0.01] |= flag.atm    # flag 4 for telluric
            sp.bpmap[o][skymsk(sp.w[o])>0.01] |= flag.sky    # flag 16 for sky
            tellind = tellmask(barshift(ww[o], -sp.berv)) > 0.01
            skyind = skymsk(barshift(ww[o], -sp.berv)) > 0.01
            #idx = sp.bpmap[o] np.searchsorted(w2,ww[o])
            pind, = where(bb[o]|tellind|skyind == 0)  # masks telluric in both spectra
            w2 = barshift(sp.w[o],sp.berv)
            b2 = sp.bpmap[o] | msksky[o]  # not used; crashes if nan present
            f2 = sp.f[o]
            idx = where((b2 & flag.nan) == 0)
            idx = where(b2 == 0)
            k2 = spline_cv(w2[idx], f2[idx])
            #gg = spl._pspline3(w2,f2, ff[o].size, w=we[ind], lam=0.002, x0=ww[o])
            if inst == 'FEROS':   # filter
               hh = np.argsort(ff[o]); ii=hh[0:len(hh)*0.98]
               pind = np.intersect1d(pind, ii)
            if not len(pind): stop('no pind')
            if 0:#  in  ee[o][pind]: #-1 in look or o in look:
               gplot(w2, f2,' us 1:2 w lp, 0,',ww[o],ff[o])
               gplot(spt.w[o],spt.f[o], 'us 0:2 w lp,',w2, f2,' us 0:2 w lp, 0,',w2, f2*(b2 == 0),' us 0:2')
               pause()

            # no clipping iterations?
            par,fmod,keep,stat = fitspec(w2, f2, k2, ww[o],ff[o], e_y=ee[o],
                                         v=0., vfix=vtfix, keep=pind, deg=deg)

            if o in lookp: #i==3
               gplot(ww[o], ff[o], ' t "ww,ff ",', ww[o][pind], ff[o][pind], ' t "ww[pind],ff[pind]"')
               ogplot(ww[o][keep], ff[o][keep], ' t "ww[keep],ff[keep]"')
               ogplot(barshift(spt.w[o],spt.berv), spt.f[o], 't "spt.w,spt.f"', flush='')
               ogplot(ww[o], fmod,' t "ww[o],fmod"')
               pause(i, o, 'RV', par.params[0]*1000.)

            rv[i,o] = rvo = par.params[0]*1000. #+ sp.drift
            e_rv[i,o] = par.perror[0] * stat['std'] * 1000
            if verb: print "%s-%02u %s %7.2f m/s %.2f  %5.1f %s" % (
                   i+1, o, sp.timeid, rvo, stat['std'], stat['snr'], par.niter)

            if coadd == "post": # save everything temporarily
               coeffs[i][o] = par.params
               mod[i,o] = fmod
               bmod[i,o] = (fmod<0) * flag.neg
               (bmod[i,o])[tellmask(barshift(ww[o],-sp.berv))>0.01] |= flag.atm
               #(emod[i,o])[bmod[i,o]==0] = (fmod[bmod[i,o]==0]/par.params[1])**0.5
               emod[i,o] = par.params[1]/fmod * (1.- tellmask(barshift(ww[o],-sp.berv))) # weights!!!!

               #gplot(w2,f2); ogplot(ww[o],fmod/par.params[1])
            if coadd == "fly":
               #norm[o] += 1./par.params[1]
               #keep=~tellind
               keep = arange(osize)   # ???????
               norm[o] += norm[o]/par.params[1]
               #bad = setdiff1d(range(len(ff[o])),keep)
               bad = np.ones(osize, dtype=bool); bad[keep]=0
               new = keep[nn[o][keep]==0]
               nn[o][keep] += 1                                # number of used spexels
               ff[o][keep] += fmod[keep]/par.params[1]         #
               ff[o][bad] *= (1.+1./par.params[1])             # do not add bad
               ff[o][new] = fmod[new]*(1.+1./par.params[1])    # replace when new
               if review>1 and o==60:
                  ogplot(ff[o]/max(ff[o]))
                  pause()

         ind = e_rv[i] > 0.                  # do not use the failed orders
         RV[i], e_RV[i] = wsem(rv[i,ind], e=e_rv[i,ind])  # weight
        print '%s/%s'%(i+1,nspecok), sp.bjd, sp.timeid, ' preRV =', RV[i], e_RV[i]
        print >>myunit, sp.bjd, 0. if -RV[i]==0 else -RV[i], e_RV[i]   # -0.0 written as 0, and read back as int not float
        myunit.flush()
        if i>2:
           gplot('"'+prefile+'" us ($1-2450000):2:3 w e pt 7')
       myunit.close()
       # end measure pre-RVs

      if coadd == 'post3':
         print 'coadding method: post3'
         npix = len(spt.w[0,:])
         ntset = len(spoklist[tset])
         wmod = zeros((ntset,npix))
         mod = zeros((ntset,npix))
         emod = zeros((ntset,npix))
         bmod = zeros((ntset,npix), dtype=int)
         spt.header['HIERARCH SERVAL OFAC'] = (ofac, 'oversampling factor per raw pixel')
         spt.header['HIERARCH SERVAL PSPLLAM'] = (pspllam, 'smoothing value of the psline')
         spt.header['HIERARCH SERVAL UTC'] = (datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"), 'time of coadding')
         for o in corders:
            print "coadding o %02i:" % o,     # continued below in iteration loop
            for i,sp in enumerate(spoklist[tset]):
             '''get the polynomials'''
             if not sp.flag:
               sp = sp.get_data(pfits=2, orders=o)
               if inst == 'FEROS':
                  thisnpix = len(sp.bpmap)
                  # spectra can have different size
                  if thisnpix < npix:
                     sp.bpmap = np.append(sp.bpmap, np.ones(npix-thisnpix))
                     sp.e = np.append(sp.e, np.ones(npix-thisnpix))
                     sp.w = np.append(sp.w, sp.w[-1]*np.ones(npix-thisnpix))
                     sp.f = np.append(sp.f, np.ones(npix-thisnpix))
                  if thisnpix > npix:
                     sp.bpmap = sp.bpmap[:npix-thisnpix]
                     sp.e = sp.e[:npix-thisnpix]
                     sp.w = sp.w[:npix-thisnpix]
                     sp.f = sp.f[:npix-thisnpix]

               bmod[i] = sp.bpmap | msksky[o]
               bmod[i][tellmask(sp.w)>0.01] |= flag.atm
               bmod[i][skymsk(sp.w)>0.01] |= flag.sky
               w2 = redshift(sp.w, vo=sp.berv, ve=-RV[i]/1000.)   # correct also for stellar rv
               i0 = np.searchsorted(w2, ww[o].min()) - 1   # w2 must be oversized
               ie = np.searchsorted(w2, ww[o].max())
               pind, = where(bmod[i][i0:ie] == 0)
               bmod[i][:i0] |= flag.out
               bmod[i][ie:] |= flag.out

               if inst == 'FEROS':
                   uind = pind*1
                   hh = np.argsort(sp.f[i0:ie])
                   ii = hh[0:len(hh)*0.98]
                   pind = np.intersect1d(pind, ii)
                   #gplot(sp.w[i0:ie], sp.f[i0:ie],',',sp.w[i0:ie][uind],sp.f[i0:ie][uind],',',sp.w[i0:ie][pind], sp.f[i0:ie][pind])
               #if o==29 and i==4: stop()
               if not len(pind):
                  print 'no valid points in n=%s, o=%s, RV=%s; skipping order' % (i, o, RV[i])
                  pause()
                  break

               # get poly from fit with mean RV
               par, fmod, keep, stat = fitspec(ww[o], ff[o], kk[o],
                  w2[i0:ie], sp.f[i0:ie], sp.e[i0:ie], v=0, vfix=True, keep=pind, v_step=False, clip=kapsig, nclip=nclip, deg=deg)   # RV  (in dopshift instead of v=RV; easier masking?)
               poly = calcspec(w2, *par.params, retpoly=True)
               #gplot( w2,sp.fo/poly); ogplot( w2[i0:ie],fmod/poly[i0:ie],' w lp ps 0.5'); ogplot(ww[o], ff[o],'w l')
               wmod[i] = w2
               mod[i] = sp.f / poly   # be careful if  poly<0
               emod[i] = sp.e / poly
               if 0:# o in lookt: #o==-29:
                  gplot(sp.w,sp.f,poly, ',"" us 1:3,', sp.w[i0:ie],(sp.f / poly)[i0:ie], ' w l,',ww[o], ff[o], 'w l')
                  pause()
              #(fmod<0) * flag.neg
            ind = (bmod&(flag.nan+flag.neg+flag.out)) == 0 # not valid
            tellind = (bmod&(flag.atm+flag.sky)) > 0                  # valid but down weighted
            #emod[tellind] *= 1000
            ind *= emod > 0.0
            we = 0*mod
            we[ind] = 1. / emod[ind]**2
            if atmfile and ('UNe' in atmfile or 'UAr' in atmfile or 'ThNe' in atmfile or 'ThAr' in atmfile): # old downweight scheme
               we[ind] += 0.000000001
               we[tellind] /= 10       # downweight
            elif atmwgt: # down weight with a constant factor
               # for low SN spectra or variing SN between observation, e.g. Trappist-1
               we[tellind] *= atmwgt   # downweight
            elif 0: # down weight with line depth
               # for high SN spectra, deep absorption line
               #we[tellind] *= (mod[tellind]/np.percentile(mod[ind+~tellind],95)).clip(0.02,1)**4 / 10
               fcont = np.abs(np.percentile(mod[ind&~tellind],95)*1.1)
               #fcont = quantile(mod[ind&~tellind], 0.95, w=1/emod[ind&~tellind])*1.1
 #              print fcont
               #we[tellind] = 1/(5*emod[tellind]**2 + (mod[tellind]-fcont)**2)
               #we[tellind] = 0.1/emod[tellind]**2   # for low S/N # keeps the relative S/N properties of the data
               #we[tellind] = 1/(emod[tellind]**2 + (fcont*np.log(abs(mod[tellind]/fcont).clip(1e-6)))**2) # for high S/N
               we[tellind] = 1/(emod[tellind]**2 + (fcont*np.log((np.sqrt(mod[tellind]**2+emod[tellind]**2)/fcont).clip(1e-6)))**2) # for high S/N
               #we[tellind] *= (mod[tellind]/fcont).clip(0.02,1)**4 / 10
               # old error vs new
               #gplot(wmod[ind],mod[ind], 1/np.sqrt(we[ind]), emod[ind], 'us 1:2:3 w e, "" us 1:2:4 w e')
            else:
               we[tellind] = 0.1 / ntset / (emod[tellind]**2 + np.median(emod[ind])**2)
            ind0 = ind*1

            niter = 2
            if inst == 'FEROS': niter = 3
            for it in range(niter+1):   # clip 5 sigma outliers
               ind2 = ww[o] < wmod[ind].max()  # but then the complement for ind2 in ff is from spt!!!
                                               # maybe extrapolation better

               # B-spline fit for co-adding
               #if o == 46:stop()
               mu, e_mu = None, None
               if pmu and pe_mu:
                  # use mean as an estimate for continuum of absoption spectrum
                  mu = wmean(mod[ind], w=we[ind]) if pmu is True else pmu
                  # deviation of mu should be as large or larger than mu
                  e_mu = pe_mu * mu  # 5 *mu

               smod, ymod = spl.ucbspl_fit(wmod[ind], mod[ind], we[ind], K=nk, lam=pspllam, mu=mu, e_mu=e_mu, e_yk=True, retfit=True)

               yfit = smod(ww[o][ind2])
               wko = smod.xk     # the knot positions
               fko = smod()      # the knot values
               eko = smod.e_yk   # the error estimates for knot values
               dko = smod.dk()   # ~second derivative at knots

               #pause()
               #hh = wko[1]-wko[0]
               #gg = cubicSpline.spl_evf(ww[o][ind2],(wko,fko,bb[:-2]/hh/6,dko[:-1]/hh**2/6,dd[:-1]/hh**3/6))
               #gplot(ww[o][ind2], yfit, ',', wko, fko,',',ww[o][ind2], gg)

               # normalised residuals
               # including telluric and original values gives conservative limits
               res = (mod[ind]-ymod) / emod[ind]
               # original values gives conservative limits
               #res = (mod[ind]-ymod) * np.sqrt(we[ind])
               #sig = std(res)
               sig = std(res[~tellind[ind]])
               # iqr(res, sig=True) untested, will be slower (in normal mode) than std but fewer iterations
               #gplot(wmod[ind], res,', %s lt 3, %s lt 3, %s lt 2, %s lt 2' %(-sig,sig,-ckappa[0]*sig, ckappa[1]*sig))

               if np.isnan(sig):
                  msg ='nan err_values in coadding. This may happen when data have gaps e.g. due masking or bad pixel flaging. Try the -pspline option'
                  if safemode:
                     print msg
                     exit()
                  pause(msg)
                  gplot(wmod[ind], mod[ind], we[ind])
               if 1:
                  # flexible sig
                  # sig = np.sqrt(spl._ucbspl_fit(wmod[ind], res**2, K=nk/5)[0])  # not good, van be negative
                  # get fraction of the data to each knot for weighting
                  G, kkk = spl._cbspline_Bk(wmod[ind], nk/5)
                  chik = np.zeros(nk/5+2)   # chi2 per knot
                  normk = np.zeros(nk/5+2)  # normalising factor to compute local chi2_red
                  for k in range(4):
                     normk += np.bincount(kkk+k, G[k], nk/5+2)
                     chik += np.bincount(kkk+k, res**2 * G[k], nk/5+2)

                  vara = spl.ucbspl(chik/normk, wmod[ind].min(), wmod[ind].max())
                  sig = np.sqrt(vara(wmod[ind]))

                  if 0 and o==33:
                     # show adaptive clipping threshold
                     gplot(wmod[ind], res, -sig, sig,  -sig*ckappa[0], sig*ckappa[1], ', "" us 1:3 w l lt 3, "" us 1:4 w l lt 3, ""  us 1:5 w l lt 7, ""  us 1:6 w l lt 7')
                     pause('look ',o)
               # pause('look ',o)

               okmap = np.array([True] * len(res))
               if ckappa[0]: okmap *= res > -ckappa[0]*sig
               if ckappa[1]: okmap *= res < ckappa[1]*sig
               okmap[tellind[ind]] = True # Oh my god. Do not reject the tellurics based on emod. That likely gives gaps and then nans.

               print "%.5f (%d)" % (np.median(sig), np.sum(~okmap)),
               #gplot(wmod[ind], res,',', wmod[ind][tellind[ind]], res[tellind[ind]])
               #pause()
               if it < niter: ind[ind] *=  okmap

            # estimate the number of valid points for each knot
            edges = 0.5 * (wko[1:]+wko[:-1])
            edges = np.hstack((edges[0]+2*(wko[0]-edges[0]), edges, edges[-1]+2*(wko[-1]-edges[-1])))
            bko,_ = np.histogram(wmod[ind], bins=edges, weights=(bmod[ind]==0)*1.0)

            '''estimate S/N for each spectrum and then combine all S/N'''
            sn = []
            yymod = mod * 0
            yymod[ind] = ymod
            for i,sp in enumerate(spoklist[tset]):
               if sp.sn55 < 400:
                  spt.header['HIERARCH COADD FILE %03i' % (i+1)] = (sp.timeid, 'rv = %0.5f km/s' % (-RV[i]/1000.))
                  iind = (i, ind[i])   # a short-cut for the indexing
                  signal = wmean(mod[iind], 1/emod[iind]**2)  # the signal
                  noise = wrms(mod[iind]-yymod[iind], emod[iind])   # the noise
                  sn.append(signal/noise)
            sn = np.sum(np.array(sn)**2)**0.5

            print ' S/N: %.5f' % sn
            spt.header['HIERARCH SERVAL COADD SN%03i' % o] = (float("%.3f" % sn), 'signal-to-noise estimate')

            # plot the model and spt
            if o in lookt:
               gplot.bar(0).key("tit '%s order %s'"% (obj,o))
               #gplot(wmod[ind],mod[ind], 1/np.sqrt(we[ind]), emod[ind], 'us 1:2:3 w e lt 2, "" us 1:2:4  w e pt 7 ps 0.5 lt 1')
               gplot(wmod[ind], mod[ind], emod[ind], ' w e pt 7 ps 0.5 t "data"', flush='')
               ogplot(ww[o], ff[o], yfit, 'w lp lt 2 ps 0.5 t "spt", "" us 1:3 w l lt 3 t "template"', flush='')
               if (~ind).any():
                  ogplot(wmod[~ind], mod[~ind], emod[~ind].clip(0,mod[ind].max()/20),'us 1:2:3 w e lt 4 pt 7 ps 0.5 t "flagged"', flush='')
               if (ind<ind0).any():
                  ogplot(wmod[ind<ind0], mod[ind<ind0], emod[ind<ind0].clip(0,1000),' w e lt 5 pt 7 ps 0.5 t "clipped"', flush='')
               if tellind.any():
                  ogplot(wmod[tellind],mod[tellind],' us 1:2 lt 6 pt 7 ps 0.5 t "atm"', flush='')
               ogplot(wko, fko, eko.clip(0,fko.max()), 'w e lt 3 pt 7 t "template knots"')
               if 0: # overplot normalised residuals
                  gplot_set('set y2tics; set ytics nomir; set y2range [-5*%f:35*%f]; set bar 0.5'%(sig,sig))
                  ogplot(wmod[ind], res,' w p pt 7 ps 0.5 lc rgb "black" axis x1y2')
               pause('lookt ',o)
               # plot relative residuals
               if 0:
                  gplot(wmod[ind], res, -sig*ckappa[1], -sig, sig, sig*ckappa[1], 'us 1:2, "" us 1:3 w l lt 2, "" us 1:4 w l lt 3, "" us 1:5 w l lt 3, "" us 1:6 w l lt 2')
                  pause('lookt ',o)


            # apply the fit
            ff[o][ind2] = yfit
            wk[o] = wko
            fk[o] = fko
            ek[o] = eko
            bk[o] = bko

      if coadd == "post2":
         for o in corders:
            mod = zeros((nspecok,osize))
            emod = zeros((nspecok,osize))
            bmod = zeros((nspecok,osize),dtype=int)
            print "coadding order %02i" % o
            for i,sp in enumerate(spoklist[tset]):
               #sp = copy.deepcopy(sp)
               sp = sp.get_data(pfits=2, orders=o)
               pind, = where(bb[o]==0)
               w2 = barshift(sp.w, sp.berv)
               b2 = sp.bpmap
               f2 = sp.f
               #k2 = interpolate.splrep(w2,f2,s=0)
               k2 = spline_cv(w2,f2)
               if o==44: pause(o)
               par,fmod,keep,stat = fitspec(
                  w2, f2, k2, ww[o], ff[o], ee[o], v=RV[i]/1000., vfix=True, keep=pind, deg=deg)
               mod[i] = fmod
               bmod[i] = (fmod<0) * flag.neg
               (bmod[i])[tellmask(barshift(ww[o],-sp.berv))>0.01] |= flag.atm
               #(emod[i,o])[bmod[i,o]==0] = (fmod[bmod[i,o]==0]/par.params[1])**0.5
               #emod[i] = par.params[1]/fmod * (1.- tellmask(barshift(ww[o],-sp.berv))) # weights!!!!
               #emod[i] = 1./par.params[1]/fmod * (1.- tellmask(barshift(ww[o],-sp.berv))) # weights!!!! problematic: role template and obs is swapped:  use ff[o] instead of fmod;
               stop()
               emod[i] = par.params[1]/ff[o] * (1.- np.max(np.vstack(tellmask(barshift(ww[o],-sp.berv)), skymsk(barshift(ww[o],-sp.berv)), axis=1))) # weights!!!!  error = ff[o]/par.params[1]
               if inst=='CARM': # work around
                  # do not allow for negative and zero weights
                  # for low S/N there is readout noise
                  bla=(ff[o]/par.params[1]).clip(min=0)+0.01
                  emod[i] = 1/bla * (1.- tellmask(barshift(ww[o],-sp.berv))) # weights!!!!  error = ff[o]/par.params[1]
               print i,par.params[1]

            emod[emod<0] *= 0.   #weights!!!
            if  pspllam is not None:
               #ff[o],rr,rg = spl._pspline3(np.tile(ww[o],(mod.shape[0],1)).flatten()*1, mod.flatten()*1,
               ff[o] = spl._pspline3(np.tile(ww[o],(mod.shape[0],1)).flatten()*1, mod.flatten()*1,
                                     ff[o].size, w=emod.flatten()+0.000000001, lam=pspllam, x0=ww[o],retcoeff=True)
               if o==50: pause(o)
            else:
               weights=emod[:]
               pixmask = np.sum(weights,axis=0) > 0
               idx, = np.where(pixmask)
               pixmask, = np.where(pixmask)
               ff[o,pixmask] = average(mod[:,pixmask],weights=weights[:,pixmask],axis=0)

            #gplot(pixmask,mod[1,pixmask],mod[2,pixmask],mod[5,pixmask], " us 1:2, '' us 1:3, '' us 1:4")
            #idx = np.tile(idx,mod.shape[0])
            #gg = pspline(idx,mod[:,pixmask].reshape(-1),K=10000,lam=0.01,w=weights[:,pixmask].reshape(-1))
            #gg=pspline(idx,mod[:,pixmask].reshape(-1),K=10000,lam=0.01)
            #gplot(idx,mod[:,pixmask].reshape(-1),np.sqrt(weights[:,pixmask].reshape(-1))*10,gg1, 'us 1:2:3 w circles lw 0.5,"" us 1:2  w l lt 1,"" us 1:4 w l')
            #gplot(idx,mod[:,pixmask].reshape(-1),np.sqrt(weights[:,pixmask].reshape(-1))*100,gg1, 'us 1:2:3 w lp ps  variable,"" us 1:4 w l')
            #gplot(idx,mod[:,pixmask].reshape(-1),gg, ' w p ps 0.5,"" us 1:3  w l lt 7')
            #ogplot(pixmask,ff[o,pixmask],savitzky_golay(ff[o],21,5)[pixmask], ' us 1:2 w l t "av", "" us 1:3 w l t "SG"' )
            #ogplot(idx,gg, 'w l')
               ff[o] = savitzky_golay(ff[o],21,5) #interpolate.UnivariateSpline(ww[o],ff[o],w=weights,s=400000)
               if pspllam is not None:
                  ff[o] = spl._pspline3(1*ww[o],ff[o],ff[o].shape[0],w=ff[o]*0+1,lam=1.0)
                #ff[o] = spl._pspline3(np.tile(ww[o],(21,1)).flatten(), mod.flatten(), ff[o].size,  w=weights.flatten()+0.000000001,lam=1.0, x0=ww[o])
            #gplot(mod[0],mod[5],mod[10],mod[15],mod[20],mod[12],ff[o],"us 0:1 ps 0.5,'' us 0:2 ps 0.5,'' us 0:3 ps 0.5,'' us 0:4 ps 0.5,'' us 0:5 ps 0.5,'' us 0:6 ps 0.5,'' us 0:7  w l lt 7")
            if 0:
               gplot(np.tile(ww[o],(21,1)).flatten(),mod.flatten(),",",ww[o],mod[12],ff[o],"us 1:2 ps 0.5,'' us 1:3 w l lt 7")
            #pause(o)


      if coadd == "post":
         emod[emod<0] *= 0.   #weights!!!
         for o in orders:   # plot the new template and compare with the old
           print "coadding order %02i" % o
           #pause(o)
           if review:
              gplot(np.mean(mod[:,o],axis=0), "w lp pt 7 lt 1 ps 0.7 t 'mean'", flush='')
              #pause()
           #weights = 0.*bmod[:,o]
           #weights[bmod[:,o]==0] = 1. /((emod[:,o])[bmod[:,o]==0])**2
           weights = emod[:,o]
           pixmask = np.sum(weights,axis=0) > 0
           ff[o,pixmask] = average(mod[:,o,pixmask],weights=weights[:,pixmask],axis=0)
           ff[o] =  savitzky_golay(ff[o],21,5)
           #interpolate.UnivariateSpline(ww[o],ff[o],w=weights,s=400000)
           ## ??????? interpolation over masked pixels?
           #ff[o] = mean(mod[:,o],axis=0)
           if review>0:
              ogplot(ff[o], "w lp pt 7 lt 7 ps 0.7 t 'SG'", flush='')
              #pause()
              #ogplot(ff[o], "w lp pt 7 lt 3 ps 0.7; unset key")
              for i,sp in enumerate(spoklist[tset]):
                 #ogplot(mod[i,o], "w p ps 0.2 ")#/coeffs[i][o][1])
                 #pause()
                 ogplot(mod[i,o],bmod[i,o]==0, "us 0:1 w p ps 0.2, '' us 0:($1/$2) w p pt 1 lt 1 ps 1 ")
                 pause(o,i)
         del mod, emod, bmod

      if isinstance(ff, np.ndarray) and np.isnan(ff.sum()): stop('nan in template')
      spt.header['HIERARCH SERVAL COADD TYPE'] = (coadd, 'coadd method')
      spt.header['HIERARCH SERVAL COADD OMIN'] = (omin, 'minimum order for RV')
      spt.header['HIERARCH SERVAL COADD OMAX'] = (omax, 'maximum order for RV')
      spt.header['HIERARCH SERVAL COADD COMIN'] = (comin, 'minimum coadded order')
      spt.header['HIERARCH SERVAL COADD COMAX'] = (comax, 'maximum coadded order')
      spt.header['HIERARCH SERVAL COADD NUM'] = (nspecok, 'number of spectra used for coadd')

      write_template(tpl, ff, ww, spt.header, hdrref='', clobber=1)
      # only post3
      write_res(outdir+obj+'.fits', {'spec':fk, 'sig':ek, 'wave':wk, 'nmap':bk}, tfmt, spt.header, hdrref='', clobber=1)
      os.system("ln -sf " + os.path.basename(tpl) + " " + outdir + "template.fits")
      print '\ntemplate written to ', tpl
      if 0: os.system("ds9 -mode pan '"+tpl+"[1]' -zoom to 0.08 8 "+tpl+"  -single &")

      print "time: %s\n" % minsec(time.time()-to)


   if review:
      for o in orders:   # plot the new template and compare with the old
         #gplot(barshift(spt.w[o],spt.berv), spt.f[o], "w lp")#,flush='')
         gplot(ww[o], ff[o]/norm[o], w='w lp pt 7 ps 0.5')
         pause(o)


   lstarmask = nomask # only 1D arrays
   do_reg = False
   if do_reg: reg = np.ones(ff.shape,dtype=bool)

   if ccf:
      pass
   elif ordwrappedtemplate:
      for o in orders:
         ii = np.isfinite(ff[o])
         kk[o] = spline_cv(ww[o][ii],ff[o][ii])
         if do_reg:
            print 'q factor masking order ', o
            reg[o] = qfacmask(ww[o],ff[o]) #, plot=True)

   if do_reg:
      idx = reg!=-1
      aa = ww[idx]
      bb = 1.0 * reg[idx]
      aaorg = 1.0 * aa.flatten()
      bborg = 1.0 * bb.flatten()
      idx = np.argsort(aa.flatten())
      aa = aa.flatten()[idx]
      bb = bb.flatten()[idx]

      #pos1, = where(bb)
      #for i, posi in enumerate(pos1[:-1]):
      #if (aa[pos1[i+1]]-aa[posi]) <(3./100000.): bb[posi:pos1[i+1]] = 1.0
      #idx1g = aa[idx1[1:]]-aa[idx1[:-1]]<3./(100000.)

      #compress mask
      def compress(aa,bb):
         '''keep only points with pre- or post-gradient'''
         idx = np.empty_like(bb,dtype=bool)
         idx[1:] = bb[1:]-bb[:-1] != 0      # pre-gradient
         idx[:-1] += bb[:-1]-bb[1:] != 0    # or post-gradient
         idx[[0,-1]] = True                 # keep always the endpoints
         return aa[idx],bb[idx]

      aa, bb = compress(aa, bb)

      # remove too small masked continuum regions (likely in order overlap)
      idx, = where(bb==1)
      dw = aa[idx[1:]]-aa[idx[:-1]]
      iidx = dw < (3./100000.)          # 3 resolution elements
      iidx = np.concatenate((idx[iidx]+1,idx[1:][iidx]-1))
      bb[iidx] = 1
      aa, bb = compress(aa, bb)
      #gplot(aaorg, bborg, 'w lp'); ogplot(aa,bb, 'w lp')
      #gplot(aa,bb); ogplot(aa[idx],bb[idx], 'w lp')
      #lstarmask = interp(ww.flatten()[idx], reg.flatten()[idx]*1.0) # only 1D arrays
      aa[0]=1 # extend the range
      aa[-1]=12
      lstarmask = interp(aa,bb) # only 1D arrays
      #lstarmask = interpolate.interp1d(aa, bb)
      #pause()


   ### Least square fitting
   results = dict((sp.timeid,['']*nord) for sp in splist)
   table = nans((7,nspec))
   bjd, RV, e_RV, rvm, rvmerr, RVc, e_RVc = table   # get pointer to the columns
   CRX, e_CRX = nans((2,nspec))   # get pointer to the columns
   mlRV, e_mlRV = nans((2,nspec))
   mlRVc, e_mlRVc = nans((2,nspec))
   mlCRX, e_mlCRX = nans((2,nspec))
   tCRX = np.rec.fromarrays(nans((5,nspec)), names='CRX,e_CRX,a,e_a,l_v' )   # get pointer to the columns
   xo = nans((nspec,nord))

   snr = nans((nspec,nord))
   rchi = nans((nspec,nord))

   if tplrv == 'targ':
      tplrv = targ.rv
      print 'setting tplrv to simbad RV:', tplrv, 'km/s'
   if tplrv == 'auto':
      tplrv = spt.ccf.rvc
      if np.isnan(tplrv):
         print 'tplrv in spt is NaN, trying median'
         rvdrs = np.array([sp.ccf.rvc for sp in spoklist])
         tplrv = np.median(rvdrs[np.isfinite(rvdrs)])
      if np.isnan(tplrv):
         print 'tplrv is NaN in all spec, simbad RV'
         tplrv = targ.rv
      print 'setting tplrv to:', tplrv, 'km/s'

   meas_index = tplrv is not None and 'B' not in fib #and not 'th_mask' in ccf
   meas_CaIRT = meas_index and inst=='CARM_VIS'
   meas_NaD = meas_index and inst=='CARM_VIS'

   if tplrv is None: tplrv = 0   # do this after setting meas_index
   tplrv = float(tplrv)
   if targrv is None:
      targrv = tplrv

   if meas_index:
      halpha = []
      haleft = []
      harigh = []
      cai = []
      cak = []
      cah = []
      irt1 = []
      irt1a = []
      irt1b = []
      irt2 = []
      irt2a = []
      irt2b = []
      irt3 = []
      irt3a = []
      irt3b = []
      nad1 = []
      nad2 = []
      nadr1 = []   # NaD Ref 1
      nadr2 = []
      nadr3 = []

   rvccf, e_rvccf = zeros((nspec,nord)), zeros((nspec,nord))
   diff_rv = bool(driftref)
   chi2map = [None] * nord
   chi2map = nans((nord, int(np.ceil((v_hi-v_lo)/ v_step))))
   diff_width = not (ccf or diff_rv)
   dLWo, e_dLWo = nans((2, nspec, nord)) # differential width change
   dLW, e_dLW = nans((2, nspec)) # differential width change

   print "RV method: ", 'CCF' if ccf else 'DRIFT' if diff_rv else 'LEAST SQUARE'

   for i,sp in enumerate(spoklist):
      #if sp.flag:
         #continue
         # introduced for drift measurement? but then Halpha is not appended and writing halpha.dat will fail
      sp = copy.deepcopy(sp)  # to prevent attaching the data to spoklist
      if sp.filename.endswith('.gz') and sp.header:
         # for gz and if deepcopy and if file already open (after coadding header still present) this will result in "AttributeError: 'GzipFile' object has no attribute 'offset'"
         # deepcopy probably does not copy everything properly
         sp.header = None
      sp.read_data()
      bjd[i] = sp.bjd

      if wfix: sp.w = spt.w
      fmod = sp.w * np.nan
      for o in orders:
         w2 = sp.w[o]
         x2 = np.arange(w2.size)
         f2 = sp.f[o]
         e2 = sp.e[o]
         b2 = sp.bpmap[o] | msksky[o]

         if inst == 'FEROS':
            pmin = pomin[o]
            pmax = pomax[o]

         #if inst=='FEROS' and fib!='B':  # filter
            #hh = np.argsort(sp.f[o]); ii=hh[0:len(hh)*0.98]; pind=np.intersect1d(pind, ii)
         b2[:pmin] |= flag.out
         b2[pmax:] |= flag.out
         b2[tellmask(w2)>0.01] |= flag.atm    # flag 8 for telluric
         b2[skymsk(w2)>0.01] |= flag.sky    # flag 16 for telluric
         b2[(tellmask(barshift(w2, -spt.berv+sp.berv+(tplrv-targrv)))>0.01)!=0] |= flag.badT   # flag 128 for bad template
         #pause()
         #if inst == 'HARPS':
            #b2[lstarmask(barshift(w2,sp.berv))>0.01] |= flag.lowQ
            #pause()
         pind = x2[b2==0]

         wmod = barshift(w2, np.nan_to_num(sp.berv))   # berv can be NaN, e.g. calibration FP, ...
         if debug:   # check the input
            gplot(dopshift(ww[o],tplrv), ff[o], ',', dopshift(wmod,targrv), f2)
            pause(o)
         rchio = 1

         if ccf:
            '''METHOD CCF'''
            f2 *= b2==0
            par, f2mod, vCCF, stat = CCF(lam2wave(ccfmask[:,0]), ccfmask[:,1], wmod, f2, targrv+v_lo, targrv+v_hi, e_y2=e2, keep=pind, plot=(o in look)+2*(o in lookssr), ccfmode=ccfmode)

            rvccf[i,o] = par.params[0] * 1000
            e_rvccf[i,o] = par.perror[0] * 1000
            keep = pind
            rchio = 1
            #pause(rvccf[i,o], e_rvccf[i,o])
         elif diff_rv:
            '''METHOD DRIFT MEASUREMENT'''
            # Correlate the residuals with the first derivative.
            # spt.w[o] and wmod are ignored, i.e. correction for sp.berv
            # We estimate the gradients using finite differences. NaN will propagate to neighbours!?
            #stop()
            #toosharp = spt.bpmap[o] * 0
            # flag template with too sharp pixels and likely cosmics or overspill
            spt.bpmap[o][1:] |= (0.15*spt.f[o][1:] > np.abs(spt.f[o][:-1])) * flag.sat
            spt.bpmap[o][:-1] |= (0.15*spt.f[o][:-1] > np.abs(spt.f[o][1:])) * flag.sat

            b2[np.where(spt.bpmap[o][1:])[0]] |= flag.badT    # flag pixels with bad neighbours resulting in bad gradients
            b2[np.where(spt.bpmap[o][:-1])[0]+1] |= flag.badT
            # flag also the overnext, espcially if using derivative from spline ( ringing, CARM_VIS overspill)
            b2[np.where(spt.bpmap[o][2:] & flag.sat)[0]] |= flag.badT
            b2[np.where(spt.bpmap[o][:-2] & flag.sat)[0]+2] |= flag.badT
            pind = x2[b2==0]
            if 1:
               # robust pre-filtering
               rr = sp.f[o]/spt.f[o]   # spectrum ratio
               qq = quantile(rr[pind], [0.25,0.5,0.75])
               clip_lo = qq[1] - 5*(qq[1]-qq[0])
               clip_hi = qq[1] + 5*(qq[2]-qq[1])
               #gplot(pind, rr[pind], ', %s,%s,%s,%s,%s' % (tuple(qq)+(clip_lo, clip_hi)))
               b2[(b2==0) & ((rr < clip_lo) | (rr > clip_hi))] |= flag.clip
               # print 'pre-clipped:', np.sum((b2 & flag.clip) >0)
               sp.f[o][pind]/spt.f[o][pind]
               pind = x2[b2==0]
               #pause('pre-clip', o)

            if 0:
               # derivative from gradient (one side numerial derivative)
               dy = np.gradient(spt.f[o], np.gradient(spt.w[o]))
               ddy = np.gradient(dy, np.gradient(spt.w[o]))
            elif 1:
               # symmetric numerical derivative
               # may underestimate gradients
               #dy = 0 * spt.f[o]
               #dy[1:-1] = (spt.f[o][2:]-spt.f[o][:-2]) / (spt.w[o][2:]-spt.w[o][:-2])
               if tuple(map(int,np.__version__.split("."))) < (1,13):
                  dy = np.gradient(spt.f[o], np.gradient(spt.w[o]))
               else:
                  dy = np.gradient(spt.f[o], spt.w[o])
               ddy = 0 * spt.f[o]
               # ddy[1:-1] = (spt.f[o][2:]-2*spt.f[o][1:-1]+spt.f[o][:-2]) / ((spt.w[o][2:]-spt.w[o][:-2])**2 / 4) # assumes dw_i ~ dw_(i+1)
               # ddy = ((f(x+h2)-f(x))/h2 - (f(x)-f(x-h1))/h1) / ((h1+h2)/2)
               ddy[1:-1] = ((spt.f[o][2:]-spt.f[o][1:-1])/(spt.w[o][2:]-spt.w[o][1:-1]) - (spt.f[o][1:-1]-spt.f[o][:-2]) / (spt.w[o][1:-1]-spt.w[o][:-2])) / ((spt.w[o][2:]-spt.w[o][:-2])/2)
            else:
               # first and second derivative from spline interpolation
               # may overestimate gradients
               # ringing may influence gradients
               kkk = interpolate.splrep(spt.w[o], spt.f[o])
               dy = interpolate.splev(spt.w[o], kkk, der=1)
               ddy = interpolate.splev(spt.w[o], kkk, der=2)
            if 0:#o==29:
               gplot(f2, 'w p,', spt.f[o], 'w lp,', pind, f2[pind])
               pause(o)

            par, f2mod, keep, stat = fitspec(spt.w[o],spt.f[o],kk[o], wmod,f2,e2, v=targrv/1000, clip=kapsig, nclip=nclip,keep=pind, df=dy, plot=o in lookssr)

            e_vi = np.abs(e2/dy)*c*1000.   # velocity error per pixel
            e_vi_min = 1/ np.sqrt(np.sum(1/e_vi[keep]**2)) # total velocity error (Butler et al., 1996)
            #print np.abs(e_vi[keep]).min(), e_vi_min
            rchio = 1
            # compare both gradients
            #gplot(keep,e_vi[keep])
            #gplot(spt.f[o][keep], dy[keep]/((sp.f[o][2:]-sp.f[o][:-2]) / (sp.w[o][2:]-sp.w[o][:-2]))[keep-1])
            #gplot(keep, dy[keep]/((sp.f[o][2:]-sp.f[o][:-2]) / (sp.w[o][2:]-sp.w[o][:-2]))[keep-1]*50, ',', keep,sp.f[o][keep],spt.f[o][keep], ', "" us 1:3')
            #pause()

            #v = -c * np.dot(1/e2[keep]**2*dy[keep], (f2-f2mod)[keep]) / np.dot(1/e2[keep]**2*dy[keep], dy[keep]) / A**2
            #dsig = c**2 *np.dot(1/e2[keep]**2*ddy[keep], (f2-f2mod)[keep]) / np.dot(1/e2[keep]**2*ddy[keep], ddy[keep]) / A**2
            #e_dsig = c**2 * np.sqrt(1 / np.dot(1/e2[keep]**2*ddy[keep], ddy[keep]) / A**2)
            #rchi = rms(((f2-f2mod)-dsig/c**2*ddy)[keep]/e2[keep])
            #e_dsig *= 1000*rchi
            #print par.params[1],par.params[0], v, dsig*1000, e_dsig

         else:
            '''DEFAULT METHOD: LEAST SQUARE'''
            if 0:
               gplot(ww[o], ff[o], 'w l,', wmod,f2/np.mean(f2)*np.mean(ff[o]),'w lp')
               pause(o)

            # pause()
            if o==41: pind=pind[:-9]   # @CARM_NIR?
            par, f2mod, keep, stat, chi2mapo = fitspec(ww[o], ff[o], kk[o], wmod, f2, e2, v=targrv-tplrv, clip=kapsig, nclip=nclip, keep=pind, indmod=np.s_[pmin:pmax], plot=o in lookssr, deg=deg, chi2map=True)

            if diff_width:
               '''we need the model at the observation and oversampled since we need the second derivative including the polynomial'''
               #f2mod = calcspec(wmod, *par.params) #calcspec does not work when w < wtmin
               #ftmod_tmp = calcspec(ww[o], *par.params) #calcspec does not work when w < wtmin
               i0 = np.searchsorted(dopshift(ww[o],par.params[0]), ww[o][0])
               i1 = np.searchsorted(dopshift(ww[o],par.params[0]), ww[o][-1]) - 1
               ftmod_tmp = 0*ww[o]
               ftmod_tmp[i0:i1] = calcspec(ww[o][i0:i1], *par.params) #calcspec does not work when w < wtmin
               ftmod_tmp[0:i0] = ftmod_tmp[i0]
               ftmod_tmp[i1:] = ftmod_tmp[i1-1]

               '''estimate differential changes in line width ("FWHM")
                  correlate the residuals with the second derivative'''
               kkk = interpolate.splrep(ww[o], ftmod_tmp)  # oversampled grid
               dy = interpolate.splev(wmod, kkk, der=1)
               ddy = interpolate.splev(wmod, kkk, der=2)
               if not def_wlog:
                  dy *= wmod
                  ddy *= wmod**2
               v = -c * np.dot(1/e2[keep]**2*dy[keep], (f2-f2mod)[keep]) / np.dot(1/e2[keep]**2*dy[keep], dy[keep])
               dsig = c**2 * np.dot(1/e2[keep]**2*ddy[keep], (f2-f2mod)[keep]) / np.dot(1/e2[keep]**2*ddy[keep], ddy[keep])
               e_dsig = c**2 * np.sqrt(1 / np.dot(1/e2[keep]**2, ddy[keep]**2))
               drchi = rms(((f2-f2mod) - dsig/c**2*ddy)[keep] / e2[keep])
               #print par.params[1],par.params[0], v, dsig*1000, e_dsig
               if np.isnan(dsig) and not safemode: pause()
               dLWo[i,o] = dsig * 1000       # convert from (km/s) to m/s km/s
               e_dLWo[i,o] = e_dsig * 1000 * drchi
               if 0:
                  gplot(wmod,(f2-f2mod),e2, 'us 1:2:3 w e,', wmod[keep],(f2-f2mod)[keep], 'us 1:2')
                  ogplot(wmod,dsig/c**2*ddy); #ogplot(wmod,f2, 'axis x1y2')
                  pause(o, 'dLW', dLWo[i,o])

         fmod[o] = f2mod
         if par.perror is None: par.perror = [0.,0.,0.,0.]
         results[sp.timeid][o] = par
         rv[i,o] = rvo = par.params[0] * 1000. #- sp.drift
         snr[i,o] = stat['snr']
         rchi[i,o] = stat['std']
         if 1 or outchi:
            vgrid = chi2mapo[0]
            chi2map[o] = chi2mapo[1]
         e_rv[i,o] = par.perror[0] * stat['std'] * 1000
         if verb: print "%s-%02u  %s  %7.2f +/- %5.2f m/s %5.2f %5.1f it=%s %s" % (i+1, o, sp.timeid, rvo, par.perror[0]*1000., stat['std'], stat['snr'], par.niter, np.size(keep))

         clipped = np.sort(list(set(pind).difference(set(keep))))
         if len(clipped):
            b2[clipped] = flag.clip
         if not safemode and (o in look or (abs(rvo/1000-targrv+tplrv)>rvwarn and not sp.flag) or debug>1):
            if def_wlog: w2 = np.exp(w2)
            res = np.nan * f2
            res[pmin:pmax] = (f2[pmin:pmax]-f2mod[pmin:pmax]) / e2[pmin:pmax]  # normalised residuals
            b = str(stat['std'])
            gplot.key('left Left rev samplen 2 tit "%s (o=%s, v=%.2fm/s)"'%(obj,o,rvo))
            gplot.ytics('nomirr; set y2tics; set y2range [-5*%f:35*%f]; set bar 0.5'%(rchio, rchio))
            gplot.put('i=1; bind "$" "i = i%2+1; xlab=i==1?\\"pixel\\":\\"wavelength\\"; set xlabel xlab; set xra [*:*]; print i; repl"')
            gplot('[][][][-5:35]', x2, w2, f2, e2.clip(0.,f2.max()), 'us (column(i)):3:4 w errorli t "'+sp.timeid+' all"', flush='')
            ogplot(x2,w2, f2, ((b2==0)|(b2==flag.clip))*0.5, 1+4*(b2==flag.clip), 'us (column(i)):3:4:5 w p pt 7 lc var ps var t "'+sp.timeid+' telluric free"', flush='')
            ogplot(x2,w2, f2mod,(b2==0)*0.5, 'us (column(i)):3:4 w lp lt 3 pt 7 ps var t "Fmod"', flush='')
            ogplot(x2,w2, res, b2, "us (column(i)):3:4 w lp pt 7 ps 0.5 lc var axis x1y2 t 'residuals'", flush='')
            # legend with translation of bpmap, plot dummy using NaN function
            ogplot(", ".join(["NaN w p pt 7 ps 0.5 lc "+str(f) +" t '"+str(f)+" "+",".join(flag.translate(f))+"'" for f in np.unique(b2)]), flush='')

            ogplot("0 axis x1y2 lt 3 t'',"+b+" axis x1y2 lt 1,-"+b+" axis x1y2 lt 1 t ''", flush='')

            ogplot(x2,w2, ((b2&flag.atm)!=flag.atm)*40-5, 'us (column(i)):3 w filledcurve x2 fs transparent solid 0.5 noborder lc 9 axis x1y2 t "tellurics"', flush='')
            ogplot(x2,w2, ((b2&flag.sky)!=flag.sky)*40-5, 'us (column(i)):3 w filledcurve x2 fs transparent solid 0.5 noborder lc 6 axis x1y2 t "sky"')
            pause('large RV ' if abs(rvo/1000-targrv+tplrv)>rvwarn else 'look ', o, ' rv = %.3f +/- %.3f m/s   rchi = %.2f' %(rvo, e_rv[i,o], rchi[i,o]))
      # end loop over orders

      # ind = setdiff1d(where(e_rv[i]>0.)[0],[71]) # do not use the failed and last order
      ind, = where(np.isfinite(e_rv[i])) # do not use the failed and last order
      rvm[i], rvmerr[i] = np.median(rv[i,ind]), std(rv[i,ind])
      if len(ind) > 1: rvmerr[i] /= (len(ind)-1)**0.5

      # Mean RV
      RV[i], e_RV[i] = wsem(rv[i,ind], e=e_rv[i,ind])
      RVc[i] = RV[i] - np.nan_to_num(sp.drift) - np.nan_to_num(sp.sa)
      e_RVc[i] = np.sqrt(e_RV[i]**2 + np.nan_to_num(sp.e_drift)**2)
      print i+1, '/', nspec, sp.timeid, sp.bjd, RV[i], e_RV[i]

      # Chromatic trend
      if 1:
         # scipy version
         #np.polynomial.polyval(x,[a,b])
         def func(x, a, b): return a + b*x #  np.polynomial.polyval(x,a)
         #
         # x = np.mean(np.exp(spt.w) if def_wlog else spt.w, axis=1)    # lambda
         # x = 1/np.mean(np.exp(spt.w) if def_wlog else spt.w, axis=1)  # 1/lambda
         x = np.mean(spt.w if def_wlog else np.log(spt.w), axis=1)  # ln(lambda)
         xc = np.mean(x[ind])   # only to center the trend fit
         # fit trend with curve_fit to get parameter error
         pval, cov = curve_fit(func, x[ind]-xc, rv[i][ind], np.array([0.0, 0.0]), e_rv[i][ind])
         perr = np.sqrt(np.diag(cov))
         #pause()
         l_v = np.exp(-(pval[0]-RV[i])/pval[1]+xc)
         CRX[i], e_CRX[i], xo[i] = pval[1], perr[1], x
         tCRX[i] = CRX[i], e_CRX[i], pval[0], perr[0], l_v

         #coli,stat = polynomial.polyfit(arange(len(rv[i]))[ind],rv[i][ind], 1, w=1./e_rv[i][ind], full=True)
         if 0:   # show trend in each order
            gplot.log('x; set autoscale xfix; set xtic add (0'+(",%i"*10)%tuple((np.arange(10)+1)*1000)+')')
            gplot(np.exp(x[ind]), rv[i][ind], e_rv[i][ind], ' us 1:2:3 w e pt 7, %f+%f*log(x/%f), %f' % (RV[i], pval[1],l_v,RV[i]))
            pause()

      if 1: # ML version of chromatic trend
         oo = ~np.isnan(chi2map[:,0])

         gg = Chi2Map(chi2map, (v_lo, v_step), RV[i]/1000, e_RV[i]/1000, rv[i,oo]/1000, e_rv[i,oo]/1000, orders=oo, keytitle=obj+' ('+inst+')\\n'+sp.timeid, rchi=rchi[i], name='')
         mlRV[i], e_mlRV[i] = gg.mlRV, gg.e_mlRV

         mlRVc[i] = mlRV[i] - np.nan_to_num(sp.drift) - np.nan_to_num(sp.sa)
         e_mlRVc[i] = np.sqrt(e_mlRV[i]**2 + np.nan_to_num(sp.e_drift)**2)

         if lookmlRV:
            gg.plot()
            pause(i, mlRV[i], e_mlRV[i])

         mlCRX[i], e_mlCRX[i] = gg.mlcrx(x, xc, ind)
         # Yet e_mlCRX is not implemented
         e_mlCRX[i] = e_CRX[i]

         if lookmlCRX:
            gg.plot_fit()
            pause(i, CRX[i], mlCRX[i])


      # Line Indices
      vabs = tplrv + RV[i]/1000.
      kwargs = {'inst': inst, 'plot':looki}
      if meas_index:
         halpha += [getHalpha(vabs, 'Halpha', **kwargs)]
         haleft += [getHalpha(vabs, 'Haleft', **kwargs)]
         harigh += [getHalpha(vabs, 'Harigh', **kwargs)]
         cai += [getHalpha(vabs, 'CaI', **kwargs)]
         cak += [getHalpha(vabs, 'CaK', **kwargs)] if inst=='HARPS' else [(np.nan,np.nan)]
         cah += [getHalpha(vabs, 'CaH', **kwargs)] if inst=='HARPS' else [(np.nan,np.nan)]
      if meas_CaIRT:
         irt1 +=  [getHalpha(vabs, 'CaIRT1', **kwargs)]
         irt1a += [getHalpha(vabs, 'CaIRT1a', **kwargs)]
         irt1b += [getHalpha(vabs, 'CaIRT1b', **kwargs)]
         irt2 +=  [getHalpha(vabs, 'CaIRT2', **kwargs)]
         irt2a += [getHalpha(vabs, 'CaIRT2a', **kwargs)]
         irt2b += [getHalpha(vabs, 'CaIRT2b', **kwargs)]
         irt3 +=  [getHalpha(vabs, 'CaIRT3', **kwargs)]
         irt3a += [getHalpha(vabs, 'CaIRT3a', **kwargs)]
         irt3b += [getHalpha(vabs, 'CaIRT3b', **kwargs)]
      if meas_NaD:
         nad1 += [getHalpha(vabs, 'NaD1', **kwargs)]
         nad2 += [getHalpha(vabs, 'NaD2', **kwargs)]
         nadr1 += [getHalpha(vabs, 'NaDref1', **kwargs)]
         nadr2 += [getHalpha(vabs, 'NaDref2', **kwargs)]
         nadr3 += [getHalpha(vabs, 'NaDref3', **kwargs)]

      if diff_width:
         ind, = where(np.isfinite(e_dLWo[i]))
         dLW[i], e_dLW[i] = wsem(dLWo[i,ind], e=e_dLWo[i,ind])

      if 0: # plot RVs of all orders
         gplot.key('title "rv %i:  %s"' %(i+1,sp.timeid))
         gplot(orders,rv[i,orders],e_rv[i,orders],rvccf[i,orders],e_rvccf[i,orders],'us 1:2:3 w e, "" us 1:4:5 w e t "ccf", {0}, {0} - {1}, {0}+{1} lt 2'.format(RV[i],e_RV[i]))
         pause('rvo')
      if 0: # plot dLW of all orders
         gplot.key('title "dLW %i:  %s"' %(i+1,sp.timeid))
         gplot(orders, dLWo[i][orders], e_dLWo[i][orders], ' w e, %f, %f, %f lt 2'%(dLW[i]+e_dLW[i],dLW[i],dLW[i]-e_dLW[i]))
         pause('dLWo')

      if outfmt and not np.isnan(RV[i]):   # write residuals
         data = {'fmod': fmod, 'wave': sp.w, 'spec': sp.f,
                 'err': sp.e, 'bpmap': sp.bpmap}
         outfile = os.path.basename(sp.filename)
         outfile = os.path.splitext(outfile)[0] + outsuf
         if 'res' in outfmt: data['res'] = sp.f - fmod
         if 'ratio' in outfmt: data['ratio'] = sp.f / fmod

         sph = Spectrum(sp.filename, inst=inst, pfits=True, drs=drs, fib=fib, targ=targ).header
         sph['HIERARCH SERVAL RV'] = (RV[i], '[m/s] Radial velocity')
         sph['HIERARCH SERVAL E_RV'] = (e_RV[i], '[m/s] RV error estimate')
         sph['HIERARCH SERVAL RVC'] = (RVc[i], '[m/s] RV drift corrected')
         sph['HIERARCH SERVAL E_RVC'] = (e_RVc[i], '[m/s] RVC error estimate')
         write_res(outdir+'res/'+outfile, data, outfmt, sph, clobber=1)

      if outchi and not np.isnan(RV[i]):   # write residuals
         gplot.palette('defined (0 "blue", 1 "green", 2 "red")')
         gplot.xlabel('"v [m/s]"; set ylabel "chi^2/max(chi^2)"; set cblabel "order"')
         #gplot(chi2map, ' matrix us ($1*%s+%s):3:2 w l palette'%(v_step, v_lo))
         if 0:
            gplot(chi2map.T /chi2map.max(axis=1), ' matrix us ($1*%s+%s):3:2 w l palette'%(v_step, v_lo))
         outfile = os.path.basename(sp.filename)
         outfile = os.path.splitext(outfile)[0] + '_chi2map.fits'
         hdr = spt.header[0:10]
         hdr.insert('COMMENT', ('CDELT1', v_step))
         hdr['CTYPE1'] = 'linear'
         hdr['CUNIT1'] = 'km/s'
         hdr['CRVAL1'] = v_lo
         hdr['CRPIX1'] = 1
         hdr['CDELT1'] = v_step
         hdr['CTYPE2'] = 'linear'
         hdr['CRVAL2'] = 1
         hdr['CRPIX2'] = 1
         hdr['CDELT2'] = 1
         write_fits(outdir+'res/'+outfile, chi2map, hdr+spt.header[10:])

      if i>0 and not safemode:
         # plot time series
         gplot(bjd-2450000, RV, e_RV, 'us 1:2:3 w e pt 7') # explicitly specify columns to deal with NaNs
      #pause()

   rvfile = outdir+obj+fibsuf+'.dat'
   rvcfile = outdir+obj+'.rvc'+fibsuf+'.dat'
   crxfile = outdir+obj+'.crx'+fibsuf+'.dat'
   mlcfile = outdir+obj+'.mlc'+fibsuf+'.dat' # maximum likehood estimated RVCs and CRX
   srvfile = outdir+obj+'.srv'+fibsuf+'.dat' # serval top-level file
   rvunit = [file(rvfile, 'w'), file(outdir+obj+'.badrv'+fibsuf+'.dat', 'w')]
   rvounit = [file(rvofile, 'w'), file(rvofile+'bad', 'w')]
   rvcunit = [file(rvcfile, 'w'), file(rvcfile+'bad', 'w')]
   crxunit = [file(crxfile, 'w'), file(crxfile+'bad', 'w')]
   mlcunit = [file(mlcfile, 'w'), file(mlcfile+'bad', 'w')]
   srvunit = [file(srvfile, 'w'), file(srvfile+'bad', 'w')]
   mypfile = [file(rvofile+'err', 'w'), file(rvofile+'errbad', 'w')]
   snrunit = [file(snrfile, 'w'), file(snrfile+'bad', 'w')]
   chiunit = [file(chifile, 'w'), file(chifile+'bad', 'w')]
   dlwunit = [file(dfwfile, 'w'), file(dfwfile+'bad', 'w')]
   if meas_index:
      halunit = [file(halfile, 'w'), file(halfile+'bad', 'w')]
   if meas_CaIRT:
      irtunit = [file(irtfile, 'w'), file(irtfile+'bad', 'w')]
   if meas_NaD:
      nadunit = [file(nadfile, 'w'), file(nadfile+'bad', 'w')]
   for i,sp in enumerate(spoklist):
      if np.isnan(rvm[i]): sp.flag |= sflag.rvnan
      rvflag = int((sp.flag&(sflag.eggs+sflag.iod+sflag.rvnan)) > 0)
      if rvflag: 'nan RV for file: '+sp.filename
      print >>rvunit[int(rvflag or np.isnan(sp.drift))], sp.bjd, RVc[i], e_RVc[i]
      print >>rvounit[rvflag], sp.bjd, RV[i], e_RV[i], rvm[i], rvmerr[i], " ".join(map(str,rv[i]))
      print >>mypfile[rvflag], sp.bjd, RV[i], e_RV[i], rvm[i], rvmerr[i], " ".join(map(str,e_rv[i]))
      #stop()
      print >>rvcunit[rvflag], sp.bjd, RVc[i], e_RVc[i], sp.drift, sp.e_drift, RV[i], e_RV[i], sp.berv, sp.sa
      print >>crxunit[rvflag], sp.bjd, " ".join(map(str,tCRX[i]) + map(str,xo[i]))
      print >>srvunit[rvflag], sp.bjd, RVc[i], e_RVc[i], CRX[i], e_CRX[i], dLW[i], e_dLW[i]
      print >>mlcunit[rvflag], sp.bjd, mlRVc[i], e_mlRVc[i], mlCRX[i], e_mlCRX[i], dLW[i], e_dLW[i]
      print >>dlwunit[rvflag], sp.bjd, dLW[i], e_dLW[i], " ".join(map(str,dLWo[i]))
      print >>snrunit[rvflag], sp.bjd, np.nansum(snr[i]**2)**0.5, " ".join(map(str,snr[i]))
      print >>chiunit[rvflag], sp.bjd, " ".join(map(str,rchi[i]))
      if meas_index:
         print >>halunit[rvflag], sp.bjd, " ".join(map(str, lineindex(halpha[i],harigh[i],haleft[i]) + halpha[i] + haleft[i] + harigh[i] + lineindex(cai[i],harigh[i],haleft[i])))  #,cah[i][0],cah[i][1]
      if meas_CaIRT:
         print >>irtunit[rvflag], sp.bjd, " ".join(map(str, lineindex(irt1[i], irt1a[i], irt1b[i]) + lineindex(irt2[i], irt2a[i], irt2b[i]) + lineindex(irt3[i], irt3a[i], irt3b[i])))
      if meas_NaD:
         print >>nadunit[rvflag], sp.bjd, " ".join(map(str, lineindex(nad1[i],nadr1[i],nadr2[i]) + lineindex(nad2[i],nadr2[i],nadr3[i])))
   for ifile in rvunit + rvounit + rvcunit + snrunit + chiunit + mypfile:
      file.close(ifile)

   t2 = time.time() - t0
   print
   print nspec, 'spectra processed', rvfile+"  (total %s, compu %s)\n" %(minsec(t2), minsec(t2-t1))

   if not driftref and nspec>1:
      x = analyse_rv(obj, postiter=postiter, fibsuf=fibsuf, safemode=safemode)
      if safemode<2: pause('TheEnd')


def arg2slice(arg):
   """Convert string argument to a slice."""
   # We want four cases for indexing: None, int, list of ints, slices.
   # Use [] as default, so 'in' can be used.
   if isinstance(arg, str):
      arg = eval('np.s_['+arg+']')
   return [arg] if isinstance(arg, int) else arg


if __name__ == "__main__":
   default = " (default: %(default)s)."
   epilog = """\
   usage example:
   %(prog)s tag dir_or_filelist -targ gj699 -snmin 10 -oset 40:
   """
   parser = argparse.ArgumentParser(description=description, epilog=epilog, add_help=False)
   argopt = parser.add_argument   # function short cut
   argopt('obj',   help='Tag, output directory and file prefix (e.g. Object name).')
   argopt('dir_or_inputlist', help='Directory name with reduced data fits/tar or a file listing the spectra (only suffixes .txt or .lis accepted).', nargs='?')
   argopt('-targ', help='Target name looked up in star.cat.')
   argopt('-targrade', help='Target coordinates: [ra|hh:mm:ss.sss de|de:mm:ss.sss].', nargs=2, default=[None,None])
   argopt('-targpm', help='Target proper motion: pmra [mas/yr] pmde [mas/yr].', nargs=2, type=float, default=[0.0,0.0])
   argopt('-targplx', help='Target parallax', type=float, default='nan')
   argopt('-targrv', help='[km/s] Target rv guess (default=tplrv)', type=float)
   argopt('-atmmask', help='Telluric line mask ('' for no masking)'+default, default='auto', dest='atmfile')
   argopt('-atmwgt', help='Downweighting factor for coadding in telluric regions'+default, type=float, default=None)
   argopt('-brvref', help='Barycentric RV code reference', choices=brvref, type=str, default='WE')
   argopt('-msklist', help='Ascii table with vacuum wavelengths to mask.', default='') # [flux and width]
   argopt('-mskwd', help='[km/s] Broadening width for msklist.', type=float, default=4.)
   argopt('-mskspec', help='Ascii 0-1 spectrum.'+default, default='')
   argopt('-ccf',  help='mode ccf [with files]', nargs='?', const='th_mask_1kms.dat', type=str)
   argopt('-ccfmode', help='type for ccf template', nargs='?', default='box',
                      choices=['box', 'binless', 'gauss', 'trapeze'])
   argopt('-coadd', help='coadd method'+default, default='post3',
                   choices=['fly', 'post', 'post2', 'post3'])
   argopt('-coset', help='index for order in coadding (default: oset)', type=arg2slice)
   #argopt('-coset', help='index for order in coadding'+default, type=arg2slqice, default=':')
   argopt('-co_excl', help='orders to exclude in coadding (default: o_excl)', type=arg2slice)
   argopt('-ckappa', help='kappa sigma (or lower and upper) clip value in coadding. Zero values for no clipping'+default, nargs='+', type=float, default=(4.,4.))
   argopt('-deg',  help='degree for background polynomial', type=int, default=3)
   argopt('-distmax', help='[arcsec] Max distance telescope position from target coordinates.', nargs='?', type=float, const=30.)
   argopt('-driftref', help='reference file for drift mode', type=str)
   argopt('-fib',  help='fibre', choices=['','A','B','AB'], default='')
   argopt('-inst', help='instrument '+default, default='HARPS',
                   choices=['HARPS', 'HARPN', 'CARM_VIS', 'CARM_NIR', 'FEROS', 'FTS'])
   argopt('-iset', help='slice for file subset (e.g. 1:10, ::5)', default=':', type=arg2slice)
   argopt('-kapsig', help='kappa sigma clip value'+default, type=float, default=3.0)
   argopt('-last', help='use last template (-tpl <obj>/template.fits)', action='store_true')
   argopt('-look', help='slice of orders to view the fit [:]', nargs='?', default=[], const=':', type=arg2slice)
   argopt('-looki', help='list of indices to watch', nargs='*', choices=['Halpha', 'Haleft', 'Haright', 'CaI', 'HK'], default=[]) #, const=['Halpha'])
   argopt('-lookt', help='slice of orders to view the coadd fit [:]', nargs='?', default=[], const=':', type=arg2slice)
   argopt('-lookp', help='slice of orders to view the preRV fit [:]', nargs='?', default=[], const=':', type=arg2slice)
   argopt('-lookssr', help='slice of orders to view the ssr function [:]', nargs='?', default=[], const=':', type=arg2slice)
   argopt('-lookmlRV', help='chi2map and master', nargs='?', default=[], const=':', type=arg2slice)
   argopt('-lookmlCRX', help='chi2map and CRX fit ', nargs='?', default=[], const=':', type=arg2slice)
   argopt('-nclip', help='max. number of clipping iterations'+default, type=int, default=2)
   argopt('-oset', help='index for order subset (e.g. 1:10, ::5)', default={'HARPS':'10:71', 'HARPN':'10:', 'CARM_VIS':'10:52', 'CARM_NIR': ':', 'FEROS':'10:', 'FTS':':'}, type=arg2slice)
   argopt('-o_excl', help='Orders to exclude (e.g. 1,10,3)', default={"CARM_NIR":"17,18,19,20,21,36,37,38,39,40,41,42", "else":[]}, type=arg2slice)
   #argopt('-outmod', help='output the modelling results for each spectrum into a fits file',  choices=['ratio', 'HARPN', 'CARM_VIS', 'CARM_NIR', 'FEROS', 'FTS'])
   argopt('-ofac', help='oversampling factor in coadding'+default, default=1., type=float)
   argopt('-outchi', help='output of the chi2 map', nargs='?', const='_chi2map.fits')
   argopt('-outfmt', help='output format of the fits file (default: None; const: fmod err res wave)', nargs='*', choices=['wave', 'err', 'fmod', 'res', 'spec', 'bpmap', 'ratio'], default=None)
   argopt('-outsuf', help='output suffix', default='_mod.fits')
   argopt('-pmin', help='Minimum pixel'+default, default=300, type=int)
   argopt('-pmax', help='Maximum pixel'+default, default={'CARM_NIR':1800, 'else':3800}, type=int)
   argopt('-pspline', help='pspline as coadd filter [smooth value]', nargs='?', const=0.0000001, dest='pspllam', type=float)
   argopt('-pmu', help='analog to GP mean. DEfault no GP penalty. Without the mean in each order. Otherwise this value.', nargs='?', const=True, type=float)
   argopt('-pe_mu', help='analog to GP mean deviation', default=5., type=float)
   argopt('-reana', help='flag reanalyse only', action='store_true')
   argopt('-review', help='level of review template', nargs='?', type=int)
   argopt('-rvwarn', help='[km/s] warning threshold in debug'+default, default=2., type=float)
   argopt('-safemode', help='does not pause or stop, optional level 1  2 (reana)', nargs='?', type=int, const=1, default=False)
   argopt('-skippre', help='Skip pre-RVs.', action='store_true')
   argopt('-skymsk', help='Sky emission line mask ('' for no masking)'+default, default='auto', dest='skyfile')
   argopt('-snmin', help='minimum S/N (considered as not bad and used in template building)'+default, default=10, type=float)
   argopt('-snmax', help='maximum S/N (considered as not bad and used in template building)'+default, default=400, type=float)
   argopt('-starcat', help='directory or filename of target look-up table. (default: local star.cat, then servaldir/star.cat)', type=str)
   argopt('-tfmt', help='output format of the template. nmap is a an estimate for the number of good data points for each knot. ddspec is the second derivative for cubic spline reconstruction. (default: spec sig wave)', nargs='*', choices=['spec', 'sig', 'wave', 'nmap', 'ddspec'], default=['spec', 'sig', 'wave'])
   argopt('-tpl',  help="template filename or directory, if None or integer a template is created by coadding, where highest S/N spectrum or the filenr is used as start tpl for the pre-RVs", nargs='?')
   argopt('-tplrv', help='[km/s] template RV (default auto, for index measures, for phoe tpl put 0 km/s, None => no measure, targ => from simbad, auto => first from header, second from targ else consider to adapt also rvguess))', default={'CARM_NIR':None, 'else':'auto'})
   argopt('-tset',  help="slice for file subset in template creation", default=':', type=arg2slice)
   argopt('-verb', help='verbose', action='store_true')
   v_lo, v_hi, v_step = -5.5, 5.6, 0.1
   argopt('-vrange', help='velocity grid around targrv (v_lo, v_hi, v_step)'+default, nargs='*', default=(v_lo, v_hi, v_step), type=float)
   argopt('-vtfix', help='fix RV in template creation', action='store_true')

   argopt('-wfix', help='fix wavelength solution', action='store_true')
   argopt('-debug', help='debug flag', nargs='?', default=0, const=1)
   argopt('-bp',   help='break points', nargs='*', type=int)
   argopt('-pdb',  help='debug post_mortem', action='store_true')
   argopt('-cprofile', help='profiling', action='store_true')
   # use add_help=false and re-add with more arguments
   argopt('-?', '-h', '-help', '--help',  help='show this help message and exit', action='help')
   #parser.__dict__['_option_string_actions']['-h'].__dict__['option_strings'] += ['-?', '-help']

   for i, arg in enumerate(sys.argv):   # allow to parse negative floats
      if len(arg) and arg[0]=='-' and arg[1].isdigit(): sys.argv[i] = ' ' + arg
   print sys.argv
   args = parser.parse_args()
   globals().update(vars(args))
   Spectrum.brvref = brvref

   if tpl and tpl.isdigit(): tpl = int(tpl)
   if isinstance(oset, dict): oset = arg2slice(oset[inst])
   if isinstance(o_excl, dict): o_excl = arg2slice(o_excl[inst]) if inst in o_excl else []
   if isinstance(pmax, dict): pmax = pmax[inst] if inst in pmax else pmax['else']
   if isinstance(tplrv, dict): tplrv = tplrv[inst] if inst in tplrv else tplrv['else']
   if coset is None: coset = oset
   if co_excl is None: co_excl = o_excl

   if dir_or_inputlist is None:
      ## execute last command
      #with open(obj+'/lastcmd.txt') as f:
         #lastcmd = f.read()
      #os.system(lastcmd)
      x = analyse_rv(obj, postiter=postiter)
      exit()

   #if targ is None: targ = obj   # sometimes convenient, but not always (targsa request is nasty)
   if len(vrange) == 1:
      v_lo, v_hi = -vrange[0], vrange[0]
   elif len(vrange) == 2:
      v_lo, v_hi = vrange
   elif len(vrange) == 3:
      v_lo, v_hi, v_step = vrange
   elif len(vrange) > 3:
      pause('too many args for -vrange')

   if len(ckappa) == 1:
      ckappa = ckappa * 2 # list with two entries ;)

   if outfmt == []:
      outfmt = ['fmod', 'err', 'res', 'wave']

   if cprofile:
      sys.argv.remove('-cprofile')
      os.system('python -m cProfile -s time -o speed.txt $SERVAL/src/serval.py '+" ".join(sys.argv[1:]))
      os.system('~/python/zechmeister/gprof2dot.py -f pstats speed.txt|  dot -Tsvg -o callgraph.svg')
      print "speed.txt"
      exit()

   if bp:
      with open('.pdbrc', 'w') as f:
         for bp_line in bp:
             print >>f, 'break ', bp_line
      #os.system('python -m pdb '+" ".join(sys.argv))
      import pdb
      #pdb.run("pass", globals(), locals());
      print 'mode d:  logging turned off, stdout reseted'
      sys.stdout = sys.__stdout__
      pdb.set_trace()
      print "enter 'c' to continue"
   else:
      os.system('rm -f .pdbrc')

   if not pdb:
      sys.exit(serval())
   else:
      try:
         sys.exit(serval())
      except:
         print 'ex'
         import pdb, sys
         e, m, tb = sys.exc_info()
         sys.stdout = sys.__stdout__
         pdb.post_mortem(tb)

