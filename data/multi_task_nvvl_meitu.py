import pynvvl
from mxnet.gluon.data import Dataset,DataLoader
from mxnet.gluon.data.vision import transforms as T
import mxnet.gluon.nn
import cupy
import numpy as np
from mxnet import nd
import ipdb
import PIL
import os
import logging
import argparse
from lru import LRU
import multiprocessing
#import cupy.cuda.Device as Device





logger = logging.getLogger(__name__)
same_shape = False
class MeituDataset(Dataset):
    def __init__(self,
                 data_dir,
                 label_file,
                 n_frame=32,
                 crop_size=112,
                 scale_w=136,
                 scale_h=136,
                 train=True,
                 device_id=0,
                 cache_size=20,
                 mode='dense',
                 transform=None):
        super(MeituDataset,self).__init__()
        def evicted(shape,loader):
            del loader
            #print("loader del shape:",shape)

        self.datadir = data_dir
        self.label_file = label_file
        self.n_frame = n_frame
        self.crop_size=crop_size
        self.scale_w = scale_w
        self.scale_h = scale_h
        self.is_train = train
        self.max_label =0
        self.clip_list = []
        self.gpu_id = device_id # type is list
        self.load_list()
        self.nvvl_loader_dict = LRU(cache_size,callback=evicted)
        self.scene_label = list(range(0,11))+list(range(34,42))    # multi label classfication
        self.action_label = list(range(11,34)) +list(range(42,63)) # single label classification # every one sample has a action label
        self.scene_length = len(self.scene_label)
        self.action_length = len(self.action_label)
        print("scene_length is ",self.scene_length)
        print("action length is ",self.action_length)
        self.scene_dict =dict([(value,index) for index,value in enumerate(self.scene_label)])   # store  (real_label:train_label)
        self.action_dict = dict([(value,index) for index,value in enumerate(self.action_label)]) # store (real_label:train_label)

        self.transform = transform
        self.loader_crt_lock = multiprocessing.Lock()
        #self.loader_list =[pynvvl.NVVLVideoLoader(device_id=temp_id, log_level='error') for temp_id in self.gpu_ids]


    def load_list(self):
        """
        load the train list to construct a file label list
        every item in self.clip_list is (file_dir,label_list)
        :return:
        """


        with open(self.label_file,'r') as fin:
            for line in fin.readlines():
                vid_info = line.split(',')
                file_name = os.path.join(self.datadir,vid_info[0])
                labels = [int(id) for id in vid_info[1:]]
                self.max_label = max(self.max_label,max(labels))
                self.clip_list.append((file_name,labels))
            self.max_label = self.max_label + 1
        logger.info("load data from %s,num_clip_List %d,max_label %d"%(self.datadir,len(self.clip_list),self.max_label))



    def __len__(self):
        return len(self.clip_list)

    def __getitem__(self, index):
        """
        clip a short video from video and coresponding label set
        :param index:
        :return:
        """
        #temp_id = np.random.choice(self.gpu_ids, 1)[0]
        if (index%2)==0:
            self.loader_crt_lock.acquire()
            try:
                with cupy.cuda.Device(self.gpu_id):
                    cupy.get_default_memory_pool().free_all_blocks()
            except Exception as e:
                print(e)
                print('index is ',index)
            self.loader_crt_lock.release()

        video_file,tags = self.clip_list[index]
        video_shape = pynvvl.video_size_from_file(video_file) #width,height

        self.loader_crt_lock.acquire()
        loader = self.nvvl_loader_dict.get(video_shape,None)
        if loader is None:
            loader = pynvvl.NVVLVideoLoader(device_id=self.gpu_id,log_level='error')
            #print("create decoder id is ",self.gpu_id)
            self.nvvl_loader_dict[video_shape] = loader
        self.loader_crt_lock.release()

        count = loader.frame_count(video_file)
        while count<self.n_frame:
            index +=1
            video_file, tags = self.clip_list[index]
            video_shape = pynvvl.video_size_from_file(video_file)  # width,height

            self.loader_crt_lock.acquire()
            loader = self.nvvl_loader_dict.get(video_shape, None)
            if loader is None:
                loader = pynvvl.NVVLVideoLoader(device_id=self.gpu_id, log_level='error')
                #print("create decoder id is ", self.gpu_id)
                self.nvvl_loader_dict[video_shape] = loader
            self.loader_crt_lock.release()
            count = loader.frame_count(video_file)

        # start frame index
        if self.is_train:
            if count <= self.n_frame:
                frame_start = 0
            else:
                frame_start = np.random.randint(0, count - self.n_frame, dtype=np.int32)
        else:
            frame_start = (count - self.n_frame) // 2

        # rescale shape
        #if self.is_train:


        if self.is_train:
            crop_x = np.random.randint(0, self.scale_w - self.crop_size, dtype=np.int32)
            crop_y = np.random.randint(0, self.scale_h - self.crop_size, dtype=np.int32)
        else:
            crop_x = (self.scale_w - self.crop_size) // 2
            crop_y = (self.scale_h - self.crop_size) // 2  # center crop

        video = loader.read_sequence(
            video_file,
            0,
            count=self.n_frame,
            sample_mode='key_frame',
            horiz_flip=False,
            scale_height=self.scale_h,
            scale_width=self.scale_w,
            crop_y=crop_y, #along with vertical direction
            crop_x=crop_x,#along with horizontal direction
            crop_height=self.crop_size,
            crop_width=self.crop_size,
            scale_method='Linear',
            normalized=False)
        scene_label = np.zeros(shape=(self.scene_length),dtype=np.float32) # multi_label scene
        action_label = self.action_length-1                                # single label action classification
        for tag_index in tags:
            if tag_index in self.scene_label:
                scene_label[self.scene_dict[tag_index]]=1
            else:
                action_label = self.action_dict[tag_index]

        #transpose from NCHW to NHWC then to Tensor and normalized
        video = (video.transpose(0,2,3,1)/255  - cupy.array([0.485, 0.456, 0.406]))/cupy.array([0.229, 0.224, 0.225])
        video = video.transpose(3,0,1,2) # from THWC to CTHW then stack to NCTHW for 3D conv.
        np_video = cupy.asnumpy(video)
        del video
        del loader
        return nd.array(np_video),nd.array(scene_label),action_label # video,multi_label and single action_label


def get_meitu_multi_task_dataloader(data_dir,device_id=0,batch_size=2,num_workers=0,n_frame=32,crop_size=112,scale_w=128,scale_h=171,cache_size=20):
    train_label_file = os.path.join(data_dir,'DatasetLabels/short_video_trainingset_annotations.txt.082902')
    global same_shape
    if same_shape:
        train_label_file = os.path.join(data_dir,'videos','filter.txt')
    train_dataset = MeituDataset(data_dir=os.path.join(data_dir,'train_collection'),
                                 label_file= train_label_file,
                                 n_frame=n_frame,
                                 crop_size=crop_size,
                                 scale_w=scale_w,
                                 scale_h=scale_h,
                                 train=True,
                                 device_id=device_id,
                                 cache_size=cache_size)

    val_dataset = MeituDataset(data_dir=os.path.join(data_dir,'val_collection'),
                               label_file=os.path.join(data_dir,'DatasetLabels/short_video_validationset_annotations.txt.0829'),
                               n_frame=n_frame,
                               crop_size=crop_size,
                               scale_w=scale_w,
                               scale_h=scale_h,
                               train=False,
                               device_id=device_id,
                               cache_size=cache_size)
    #if __name__=='__main__':
        # print the video name for decoder error
        # index = [8*3199,3219*8]
        # for i in range(12812,12816):
        #     try:
        #         print(i)
        #         video = val_dataset[i]
        #     except Exception as e:
        #         print(e)
        #         print("the index is ",i,"file name",val_dataset.clip_list[i])
        # print(val_dataset.clip_list[12814])
        # print('finishd')
        # exit(0)
    # if __name__=='__main__':
    #     # come to debug mode
    #     import time
    #     for i in range(0,30,3):
    #         data,label = train_dataset[i]
    #         print('data index ',i,data.shape,label.shape)
    #         time.sleep(0.5)
    #     print('finish test the same shape video')
    print("the nvvl dataset loader unit test number of workers is ",num_workers)
    train_loader = DataLoader(train_dataset,batch_size=batch_size,shuffle=True,num_workers=num_workers,last_batch='discard')
    val_loader = DataLoader(val_dataset,batch_size=batch_size,shuffle=False,num_workers=num_workers,last_batch='discard')
    sample_weight = [341,264,559,159,105,673,8128,9528,5462,814,269,356,439,510,395,390,348,8447,290,322,484,278,452,\
                     2502,76,84,440,909,841,779,297,242,275,332,421,350,326,393,327,484,213,488,340,436]
    print('sample shape',len(sample_weight))
    return train_loader,val_loader,sample_weight


if __name__=='__main__':
    parser = argparse.ArgumentParser(description='this is a dataset test parser')
    parser.add_argument('--data_dir',type=str,default='/data/jh/notebooks/hudengjun/VideosFamous/FastVideoTagging/meitu',help='this is the meitu root directory')
    parser.add_argument('--gpus',type=int,default=0,help='the decoder gpus id')
    args = parser.parse_args()
    same_shape = False
    train_loader,val_loader,sample_weight = get_meitu_dataloader(data_dir=args.data_dir,
                                                    device_id=args.gpus,
                                                   batch_size=4,
                                                   num_workers=0,
                                                   n_frame=16,
                                                   crop_size=112,
                                                   scale_w=128,
                                                   scale_h=128)
    for i,(batch_data,scene_label,action_label) in enumerate(val_loader):
        print('batch_data shape',batch_data.shape)
        print('batch_label shape',scene_label)
        print('action_label',action_label)
        if i==10:
            print("test the dataloader")
            break



