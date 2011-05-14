#!/usr/bin/python

'''

    phraymd Import plugin
    Copyright (C) 2009  Damien Moore

License:

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

'''
photo importer
in gnome:
    all devices are mounted into the filesystem
    an import source is a collection (typically a directory, if a device then  a directory mounted by gio gphoto interface)
    the import destination is somewhere in the destination collection directory
    an import operation is a copy or move from source to destination
    the most basic import just copies all supported files across to a destination in the collection directory
    option to obey directory structure of source
    option to remove images from the source
    option to copy images into a date based folder structure using exif date taken
    nameclash option: don't copy or use an alternate name
    option to select images to import or import all -- use the internal browser to handle this
    dbus interface to handle user plugging in camera/device containing images (open sidebar and make device the import source)
    todo:
    * ability to set metadata
    * keep an import log -- full src and dest path of every file copied or moved + names of files ignored
    * a custom filter in the browser for viewing the most recent import
    if user browses the volume then the full scan/verify/create thumb steps are going to happen. once the users chooses which images to import we don't want to have to redo that step -- should just be able to copy the item objects across (and rename them appropriately) and reuse the thumbnails.

maybe need a gphoto alternative for non-gnome desktops
'''

import os
import os.path
import threading
import tempfile
import string
import datetime
import re

import gtk
import gobject

from phraymd import dialogs
from phraymd import settings
from phraymd import pluginbase
from phraymd import pluginmanager
from phraymd import imagemanip
from phraymd import io
from phraymd import viewsupport
from phraymd import metadata
from phraymd import backend
from phraymd import collectionmanager
from phraymd import baseobjects

exist_actions=['Skip','Rename','Overwrite Always','Overwrite if Newer']
EXIST_SKIP=0
EXIST_RENAME=1
EXIST_OVERWRITE=2
EXIST_OVERWRITE_NEWER=3


class NamingTemplate(string.Template):
    def __init__(self,template):
        print 'naming template',template
        t=template.replace("<","${").replace(">","}")
        print 'subbed naming template',t
        string.Template.__init__(self,t)

def get_date(item):
    '''
    returns a datetime object containing the date the image was taken or if not available the mtime
    '''
    result=viewsupport.get_ctime(item)
    if result==datetime.datetime(1900,1,1):
        return datetime.datetime.fromtimestamp(item.mtime)
    else:
        return result

def get_year(item):
    '''
    returns 4 digit year as string
    '''
    return '%04i'%get_date(item).year

def get_month(item):
    '''
    returns 2 digit month as string
    '''
    return '%02i'%get_date(item).month

def get_day(item):
    '''
    returns 2 digit day as string
    '''
    return '%02i'%get_date(item).day

def get_datetime(item):
    '''
    returns a datetime string of the form "YYYYMMDD-HHMMSS"
    '''
    d=get_date(item)
    return '%04i%02i%02i-%02i%02i%02i'%(d.year,d.month,d.day,d.hour,d.minute,d.day)

def get_original_name(item):
    '''
    returns a tuple (path,name) for the transfer destination
    '''
    ##this only applies to locally stored files
    return os.path.splitext(os.path.split(item.uid)[1])[0]


class VariableExpansion:
    def __init__(self,item):
        self.item=item
        self.variables={
            'Year':get_year,
            'Month':get_month,
            'Day':get_day,
            'DateTime':get_datetime,
            'ImageName':get_original_name,
            }
    def __getitem__(self,variable):
        return self.variables[variable](self.item)

#def naming_default(item,dest_dir_base):
#    '''
#    returns a tuple (path,name) for the transfer destination
#    '''
#    return dest_dir_base,os.path.split(item.uid)[1]

def name_item(item,dest_base_dir,naming_scheme):
    subpath=NamingTemplate(naming_scheme).substitute(VariableExpansion(item))
    ext=os.path.splitext(item.uid)[1]
    fullpath=os.path.join(dest_base_dir,subpath+ext)
    return os.path.split(fullpath)

naming_schemes=[
    ("<ImageName>","<ImageName>",False),
    ("<Year>/<Month>/<ImageName>","<Year>/<Month>/<ImageName>",True),
    ("<Y>/<M>/<DateTime>-<ImageName>","<Year>/<Month>/<DateTime>-<ImageName>",True),
    ("<Y>/<M>/<Day>/<ImageName>","<Year>/<Month>/<Day>/<ImageName>",True),
    ("<Y>/<M>/<Day>/<DateTime>-<ImageName>","<Year>/<Month>/<Day>/<DateTime>-<ImageName>",True),
    ]

def altname(pathname):
    dirname,fullname=os.path.split(pathname)
    name,ext=os.path.splitext(fullname)
    i=0
    while os.path.exists(pathname):
        i+=1
        aname=name+'_(%i)'%i
        pathname=os.path.join(dirname,aname+ext)
    return pathname

class TransferImportJob(backend.WorkerJob):
    def __init__(self,worker,collection,browser,plugin,collection_src,collection_dest,params):
        backend.WorkerJob.__init__(self,'TRANSFER',780,worker,collection,browser)
        self.plugin=plugin
        self.collection_src=collection_src
        self.collection_dest=collection_dest
        self.stop=False
        self.countpos=0
        self.items=None
        for p in params:
            self.__dict__[p]=params[p]
##        self.plugin.mainframe.tm.queue_job_instance(self)

    def cancel(self,shutdown=False):
        if not shutdown:
            gobject.idle_add(self.plugin.transfer_cancelled)

    def __call__(self):
        jobs=self.worker.jobs
        worker=self.worker
        i=self.countpos
        if not self.collection_dest.is_open or not self.collection_src.is_open:
            gobject.idle_add(self.plugin.transfer_cancelled)
            return True
        collection=self.collection_dest
        if self.items==None:
            pluginmanager.mgr.suspend_collection_events(self.collection_dest)
            if self.transfer_all:
                self.items=self.collection_src.get_all_items()
                print 'transferring all',len(self.items)
            else:
                self.items=self.collection_src.get_active_view().get_selected_items()
                self.count=len(self.items)
        if not self.base_dest_dir:
            try:
                self.base_dest_dir=collection.image_dirs[0]
            except:
                self.base_dest_dir=os.environ['HOME']
        print 'transferring',len(self.items),'items'
        if not os.path.exists(self.base_dest_dir):
            os.makedirs(self.base_dest_dir)
        while len(self.items)>0 and jobs.ishighestpriority(self) and not self.stop:
            item=self.items.pop()
            ##todo: must set prefs
            if self.browser:
                gobject.idle_add(self.browser.update_status,1.0*i/self.count,'Transferring media - %i of %i'%(i,self.count))
            prefs={
                'base_dest_dir':self.base_dest_dir,
                'dest_name_needs_meta':self.dest_name_needs_meta,
                'dest_name_template':self.dest_name_template,
                'name_item_fn':name_item,
                'action_if_exists':self.action_if_exists,
                'altname_fn':altname,
                'move_files':self.move,
                'upload_size':None,
                'metadata_strip':False,
            }
            collection.copy_item(self.collection_src,item,prefs)
###            if self.browser!=None:
###                self.browser.lock.acquire()
###            print 'transferring item',item.uid,'to',collection.id
###            collection.add(item)
###            if self.browser!=None:
###                self.browser.lock.release()
            ##todo: log success
            i+=1
            if self.browser:
                gobject.idle_add(self.browser.resize_and_refresh_view)
        self.countpos=i
        if len(self.items)==0 or self.stop:
            if self.browser:
                gobject.idle_add(self.browser.update_status,2,'Transfer Complete')
            gobject.idle_add(self.plugin.transfer_completed)
            pluginmanager.mgr.resume_collection_events(self.collection)
            self.collection_src=None
            self.collection_dest=None
            #jobs['VERIFYIMAGES'].setevent()
            if self.stop:
                gobject.idle_add(self.plugin.transfer_cancelled)
            else:
                gobject.idle_add(self.plugin.transfer_completed)
            return True
        return False


class TransferPlugin(pluginbase.Plugin):
    name='Transfer'
    display_name='Image Transfer'
    api_version='0.1.0'
    version='0.1.0'
    def __init__(self):
        pass

    def plugin_init(self,mainframe,app_init):
        self.mainframe=mainframe

        def box_add(box,widget_data,label_text):
            hbox=gtk.HBox()
            if label_text:
                label=gtk.Label(label_text)
                hbox.pack_start(label,False)
            for widget in widget_data:
                hbox.pack_start(widget[0],widget[1])
                if widget[2]:
                    widget[0].connect(*widget[2:])
            box.pack_start(hbox,False)
            return tuple([hbox]+[widget[0] for widget in widget_data])

        self.vbox=gtk.VBox()
        self.vbox.set_spacing(5)
#        self.transfer_source_entry=dialogs.PathnameEntry('',
#            "From Path","Choose Transfer Source Directory")
#        self.vbox.pack_start(self.transfer_source_entry,False)
        self.src_combo=collectionmanager.CollectionCombo(mainframe.coll_set.add_model('SELECTOR'))
        self.dest_combo=collectionmanager.CollectionCombo(mainframe.coll_set.add_model('OPEN_SELECTOR'))
        box_add(self.vbox,[(self.src_combo,True,"collection-changed",self.src_changed),
                           (gtk.Button("View"),False,"clicked",self.src_view)],
                            "From: ")
        box_add(self.vbox,[(self.dest_combo,True,"collection-changed",self.dest_changed),
                           (gtk.Button("View"),False,"clicked",self.dest_view)],
                            "To: ")
#        self.vm=io.VolumeMonitor()
#        self.transfer_source_combo=dialogs.PathnameCombo("","Transfer from","Select directory to transfer from",volume_monitor=self.vm,directory=True)
#        self.vbox.pack_start(self.transfer_source_combo,False)
        ##SETTINGS

#        ##BROWSING OPTIONS -- todo: use this as a collection browsing dialog
#        self.browse_frame=gtk.Expander("Browsing Options")
#        self.browse_box=gtk.VBox()
#        self.browse_frame.add(self.browse_box)
#        self.browse_read_meta_check=gtk.CheckButton("Load Metadata")
#        self.browse_use_internal_thumbnails_check=gtk.CheckButton("Use Internal Thumbnails")
#        self.browse_use_internal_thumbnails_check.set_active(True)
#        self.browse_store_thumbnails_check=gtk.CheckButton("Store Thumbnails in Cache")
#        self.browse_box.pack_start(self.browse_read_meta_check,False)
#        self.browse_box.pack_start(self.browse_use_internal_thumbnails_check,False)
#        #self.browse_box.pack_start(self.browse_store_thumbnails_check,False) ##todo: switch this back on and implement in backend/imagemanip
#        self.vbox.pack_start(self.browse_frame,False)

        ##TRANSFER OPTIONS
        self.transfer_frame=gtk.Expander("Advanced Transfer Options")
        self.transfer_box=gtk.VBox()
        self.transfer_frame.add(self.transfer_box)
        self.base_dir_entry=dialogs.PathnameEntry('', ##self.mainframe.tm.collection.image_dirs[0]
            "To Path","Choose transfer directory")
        self.transfer_box.pack_start(self.base_dir_entry,False)

        self.name_scheme_model=gtk.ListStore(str,gobject.TYPE_PYOBJECT,gobject.TYPE_BOOLEAN) ##display, callback, uses metadata
        for n in naming_schemes:
            self.name_scheme_model.append(n)
        self.name_scheme_combo=gtk.ComboBox(self.name_scheme_model)
        box,self.name_scheme_combo=box_add(self.transfer_box,[(self.name_scheme_combo,True,None)],"Naming Scheme")
        cell = gtk.CellRendererText()
        self.name_scheme_combo.pack_start(cell, True)
        self.name_scheme_combo.add_attribute(cell, 'text', 0)
        self.name_scheme_combo.set_active(3) #todo: instead of setting a fixed default, remember users last settings (maybe per device?)

        box,self.exists_combo=box_add(self.transfer_box,[(gtk.combo_box_new_text(),True,None)],"Action if Destination Exists")
        for x in exist_actions:
            self.exists_combo.append_text(x)
        self.exists_combo.set_active(0)

        self.copy_radio=gtk.RadioButton(None,"_Copy",True)
        self.move_radio=gtk.RadioButton(self.copy_radio,"_Move",True)
        box_add(self.vbox,[(self.copy_radio,True,None),(self.move_radio,True,None)],"Transfer Operation: ")

        self.vbox.pack_start(self.transfer_frame,False)

        self.transfer_action_box,self.cancel_button,self.start_transfer_all_button,self.start_transfer_button=box_add(self.vbox,
            [(gtk.Button("Cancel"),True,"clicked",self.cancel_transfer),
             (gtk.Button("Transfer All"),True,"clicked",self.start_transfer,True),
             (gtk.Button("Transfer Selected"),True,"clicked",self.start_transfer,False)],
            "")

        self.cancel_button.set_sensitive(False)

#        self.mode_box,button1,button2=box_add(self.vbox,
#            [(gtk.Button("Transfer Now"),"clicked",self.transfer_now),
#            (gtk.Button("Transfer Now"),"clicked",self.transfer_now)],
#            "")
#        button1.set_sensitive(False)
        #button2.set_sensitive(False)

        self.scrolled_window=gtk.ScrolledWindow() ##todo: use a custom Notebook to embed all pages in a scrolled window automatically
        self.scrolled_window.set_policy(gtk.POLICY_AUTOMATIC,gtk.POLICY_AUTOMATIC)
        self.scrolled_window.add_with_viewport(self.vbox)
        self.scrolled_window.show_all()
#        self.transfer_frame.hide()
#        self.transfer_action_box.hide()

        self.dialog=self.mainframe.float_mgr.add_panel('Transfer','Show or hide the transfer panel (use it to transfer photos between collection, devices and local folders)','phraymd-transfer')
        self.dialog.set_default_size(450,300)
        self.dialog.vbox.pack_start(self.scrolled_window)

        #self.mainframe.sidebar.append_page(self.scrolled_window,gtk.Label("Transfer"))

    def plugin_shutdown(self,app_shutdown):
        if not app_shutdown:
            self.mainframe.float_mgr.remove_panel('Transfer')
            self.scrolled_window.destroy()
            del self.transfer_job
            ##todo: delete references to widgets

    def src_view(self,button):
        id=self.src_combo.get_active()
        if id:
            self.mainframe.coll_combo.set_active(id)
        self.mainframe.grab_focus() ##todo: add mainframe method "restore_focus"

    def dest_view(self,button):
        id=self.dest_combo.get_active()
        if id:
            self.mainframe.coll_combo.set_active(id)
        self.mainframe.grab_focus() ##todo: add mainframe method "restore_focus"

    def src_changed(self,combo,id):
        if combo.get_active_coll()==None:
            return
        if combo.get_active_coll()==self.dest_combo.get_active_coll():
            self.dest_combo.set_active(None)

    def dest_changed(self,combo,id):
        coll=combo.get_active_coll()
        if coll==None:
            self.base_dir_entry.set_path('')
            return
        if combo.get_active_coll()==self.src_combo.get_active_coll():
            self.src_combo.set_active(None)
        if coll:
            if coll.is_open:
                self.base_dir_entry.set_path(coll.image_dirs[0])

    def transfer_cancelled(self):
        '''
        called from the transfer job thread to indicate the job has been cancelled
        '''
        ##todo: give visual indication of cancellation
        self.transfer_frame.set_sensitive(True)
        self.src_combo.set_sensitive(True)
        self.dest_combo.set_sensitive(True)
        self.start_transfer_button.set_sensitive(True)
        self.start_transfer_all_button.set_sensitive(True)
        self.cancel_button.set_sensitive(False)

    def transfer_completed(self):
        '''
        called from the transfer job thread to indicate the job has completed
        '''
        ##todo: give visual indication of completion
        self.transfer_frame.set_sensitive(True)
        self.src_combo.set_sensitive(True)
        self.dest_combo.set_sensitive(True)
        self.start_transfer_button.set_sensitive(True)
        self.start_transfer_all_button.set_sensitive(True)
        self.cancel_button.set_sensitive(False)


#    def transfer_now(self,button):
#        self.start_transfer_button.set_sensitive(False)
#        worker=self.mainframe.tm
#        transfer_job.start_transfer(params)

    def cancel_transfer(self,button):
        self.mainframe.tm.job_queue.clear(TransferImportJob)

    def start_transfer(self,button,all):
        coll_src=self.src_combo.get_active_coll()
        coll_dest=self.dest_combo.get_active_coll()
        if coll_src==None or coll_dest==None:
            return
        self.start_transfer_button.set_sensitive(False)
        worker=self.mainframe.tm
        params={}
        params['transfer_all']=all
        params['move']=self.move_radio.get_active()
        params['base_dest_dir']=self.base_dir_entry.get_path()
        params['action_if_exists']=self.exists_combo.get_active()
        iter=self.name_scheme_combo.get_active_iter()
        if iter:
            row=self.name_scheme_model[iter]
            params['dest_name_needs_meta']=row[2]
            params['dest_name_template']=row[1]
        else:
            return
        if not coll_src.is_open:
            coll_src.open(self.mainframe.tm)
        ij=TransferImportJob(self.mainframe.tm,None,coll_dest.browser,self,coll_src,coll_dest,params)
        self.mainframe.tm.queue_job_instance(ij)
        self.cancel_button.set_sensitive(True)
        self.start_transfer_all_button.set_sensitive(False)
        self.start_transfer_button.set_sensitive(False)
        self.transfer_frame.set_sensitive(False)

    def media_connected(self,uri): ##todo: ensure that uri is actually a local path and if so rename the argument
        print 'media connected event for',uri
        sidebar=self.mainframe.sidebar
        sidebar.set_current_page(sidebar.page_num(self.scrolled_window))
        self.mainframe.sidebar_toggle.set_active(True)
        self.src_combo.set_active(uri)
        if self.mainframe.active_collection!=None:
            self.dest_combo.set_active(self.mainframe.active_collection.id)
#        if self.src_combo.get_editable():
#            self.transfer_source_combo.set_path(io.get_path_from_uri(uri))

    def open_uri(self,uri):
        self.mainframe.open_uri(uri)