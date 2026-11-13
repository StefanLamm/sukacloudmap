"""Explicit reference conversion using the same coordtransform 2.1.2 as the UI.

Port of https://github.com/wandergis/coordtransform (MIT):
Copyright (c) 2015 记忆的残骸

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import math


def provider_point(point):
    """Convert declared coordinates, preserving the original frozen data.

    Approximate reference conversion does not verify an address, entrance,
    or truck passage. Unknown/unpaired/invalid coordinates stay unusable.
    """
    if point.get('crs') not in {'GCJ02','WGS84','BD09'}:
        raise ValueError('Unknown coordinate system')
    if point.get('longitude') is None or point.get('latitude') is None:
        raise ValueError('Missing coordinates')
    x,y=float(point['longitude']),float(point['latitude'])
    if not math.isfinite(x+y) or abs(x)>180 or abs(y)>90:
        raise ValueError('Invalid coordinates')
    pi=math.pi
    if point['crs']=='BD09':
        x-=.0065;y-=.006
        k=pi*3000/180
        z=math.sqrt(x*x+y*y)-.00002*math.sin(y*k)
        theta=math.atan2(y,x)-.000003*math.cos(x*k)
        x,y=z*math.cos(theta),z*math.sin(theta)
    elif point['crs']=='WGS84' and 73.66<x<135.05 and 3.86<y<53.55:
        a,b=x-105,y-35
        dlat=-100+2*a+3*b+.2*b*b+.1*a*b+.2*math.sqrt(abs(a))
        dlat+=(20*math.sin(6*a*pi)+20*math.sin(2*a*pi))*2/3
        dlat+=(20*math.sin(b*pi)+40*math.sin(b/3*pi))*2/3
        dlat+=(160*math.sin(b/12*pi)+320*math.sin(b*pi/30))*2/3
        dlng=300+a+2*b+.1*a*a+.1*a*b+.1*math.sqrt(abs(a))
        dlng+=(20*math.sin(6*a*pi)+20*math.sin(2*a*pi))*2/3
        dlng+=(20*math.sin(a*pi)+40*math.sin(a/3*pi))*2/3
        dlng+=(150*math.sin(a/12*pi)+300*math.sin(a/30*pi))*2/3
        rad=y/180*pi;magic=1-.00669342162296594323*math.sin(rad)**2
        dlat=dlat*180/(6378245*(1-.00669342162296594323)/(magic*math.sqrt(magic))*pi)
        dlng=dlng*180/(6378245/math.sqrt(magic)*math.cos(rad)*pi)
        x+=dlng;y+=dlat
    return {'longitude':round(x,6),'latitude':round(y,6),'crs':'GCJ02'}
