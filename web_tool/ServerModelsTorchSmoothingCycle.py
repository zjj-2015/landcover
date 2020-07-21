
from web_tool.ServerModelsAbstract import BackendModel
import torch as T
import numpy as np
import torch.nn as nn
import copy
import os, json
from training.pytorch.utils.eval_segm import mean_IoU, pixel_accuracy
from torch.autograd import Variable
import time
from scipy.special import softmax

class CoreModel(nn.Module):
    def __init__(self):
        super(CoreModel,self).__init__()
        self.conv1 = T.nn.Conv2d(4,64,3,1,1)
        self.conv2 = T.nn.Conv2d(64,64,3,1,1)
        self.conv3 = T.nn.Conv2d(64,64,3,1,1)
        self.conv4 = T.nn.Conv2d(64,64,3,1,1)
        self.conv5 = T.nn.Conv2d(64,64,3,1,1)
       
    def forward(self,inputs):
        x = T.relu(self.conv1(inputs))
        x = T.relu(self.conv2(x))
        x = T.relu(self.conv3(x))
        x = T.relu(self.conv4(x))
        x = T.relu(self.conv5(x))
        return x

class AugmentModel(nn.Module):
    def __init__(self):
        super(AugmentModel,self).__init__()
        self.last = T.nn.Conv2d(64,22,1,1,0)
        
    def forward(self,inputs):
        return self.last(inputs)
    
class TorchSmoothingCycleFineTune(BackendModel):

    def __init__(self, model_fn, gpuid, fine_tune_layer, num_models):

        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpuid)
        self.output_channels = 22
        self.input_size = 240
        self.did_correction = False
        self.model_fn = model_fn
        self.device = T.device("cuda:0" if T.cuda.is_available() else "cpu")

        self.num_models = num_models
        
        self.core_model = CoreModel()
        self.augment_models = [ AugmentModel() for _ in range(num_models) ]
        
        self.init_model()

        for model in self.augment_models:
            for param in model.parameters():
                param.requires_grad = True
            print(sum(x.numel() for x in model.parameters()))

        # ------------------------------------------------------
        # Step 2
        #   Pre-load augment model seed data
        # ------------------------------------------------------
        self.current_features = None

        self.augment_base_x_train = []
        self.augment_base_y_train = []

        self.augment_x_train = []
        self.augment_y_train = []
        self.model_trained = False
        self.naip_data = None
        
        self.corr_features = [[] for _ in range(num_models) ]
        self.corr_labels = [[] for _ in range(num_models) ]
        
        self.num_corrected_pixels = 0
        self.batch_count = 0
        self.run_done = False
        self.rows = 892
        self.cols = 892

    def run(self, naip_data, naip_fn, extent):
        print(naip_data.shape)
      
        x = naip_data
        x = np.swapaxes(x, 0, 2)
        x = np.swapaxes(x, 1, 2)
        x = x[:4, :, :]
        naip_data = x / 255.0

        self.last_outputs = []

        self.naip_data = naip_data  # keep non-trimmed size, i.e. with padding

        with T.no_grad():

            features = self.run_core_model_on_tile(naip_data)
            self.features = features.cpu().numpy()

            for i in range(self.num_models):

                out = self.augment_models[i](features).cpu().numpy()[0,1:]
                out = np.rollaxis(out, 0, 3)
                out = softmax(out, 2)
            
                self.last_outputs.append(out)

        return self.last_outputs

    def retrain(self, train_steps=100, learning_rate=1e-3):
      
        print_every_k_steps = 33

        print("Fine tuning with %d new labels." % self.num_corrected_pixels)
        
        self.init_model()
        
        for model, corr_features, corr_labels in zip(self.augment_models, self.corr_features, self.corr_labels):
            batch_x = T.from_numpy(np.array(corr_features)).float().to(self.device)
            batch_y = T.from_numpy(np.array(corr_labels)).to(self.device)


            if batch_x.shape[0] > 0:
            
                optimizer = T.optim.Adam(model.parameters(), lr=learning_rate, eps=1e-5)
                
                criterion = T.nn.CrossEntropyLoss().to(self.device)

                for i in range(train_steps):
                    #print('step %d' % i)
                    acc = 0
                    
                    with T.enable_grad():

                        optimizer.zero_grad()
                        
                        pred = model(batch_x.unsqueeze(2).unsqueeze(3)).squeeze(3).squeeze(2)
                        
                        loss = criterion(pred,batch_y)
                        
                        print(loss.mean().item())
                        
                        acc = (pred.argmax(1)==batch_y).float().mean().item()

                        loss.backward()
                        optimizer.step()
                    
                    if i % print_every_k_steps == 0:
                        print("Step pixel acc: ", acc)

                    message = "Fine-tuned model with %d samples." % (len(corr_features))

        success = True
        message = "Fine-tuned models with {} samples.".format((','.join(str(len(x)) for x in self.corr_features)))
        print(message)
        return success, message
    
    def undo(self):
        pass
        #if len(self.corr_features)>0:
        #    self.corr_features = self.corr_features[:-1]
        #    self.corr_labels = self.corr_labels[:-1]
        #print('undoing; now there are %d samples' % len(self.corr_features))

    def add_sample(self, tdst_row, bdst_row, tdst_col, bdst_col, class_idx, model_idx):
        print("adding sample: class %d (incremented to %d) at (%d, %d), model %d" % (class_idx, class_idx+1 , tdst_row, tdst_col, model_idx))

        for i in range(tdst_row,bdst_row+1):
            for j in range(tdst_col,bdst_col+1):
                self.corr_labels[model_idx].append(class_idx+1)
                self.corr_features[model_idx].append(self.features[0,:,i,j])

    def init_model(self):
        checkpoint = T.load(self.model_fn, map_location=self.device)
        self.core_model.load_state_dict(checkpoint, strict=False)
        self.core_model.eval()
        self.core_model.to(self.device)
        for model in self.augment_models:
            model.load_state_dict(checkpoint, strict=False)
            model.eval()
            model.to(self.device)
        
    def reset(self):
        self.init_model()
        self.model_trained = False
        self.run_done = False
        self.num_corrected_pixels = 0

    def run_core_model_on_tile(self, naip_tile):
          
        _, w, h = naip_tile.shape
        
        out = np.zeros((21, w, h))

        x_c_tensor1 = T.from_numpy(naip_tile).float().to(self.device)
        features = self.core_model(x_c_tensor1.unsqueeze(0))
           
        return features
        

    def run_model_on_tile(self, naip_tile, model_idx, last_features=False, batch_size=32):
        
        with T.no_grad():
            if last_features:
                y_hat,features = self.predict_entire_image(naip_tile,model_idx,last_features)
                output = y_hat[:, :, :]
                return softmax(output,2),features
            else:
                y_hat = self.predict_entire_image(naip_tile,model_idx,last_features)
                output = y_hat[:, :, :]
                return softmax(output,2)

    def predict_entire_image(self, x, model_idx, last_features=False):
       
        model = T.nn.Sequential(self.core_model, self.augment_models[model_idx])

        norm_image = x
        _, w, h = norm_image.shape
        
        out = np.zeros((21, w, h))

        norm_image1 = norm_image
        x_c_tensor1 = T.from_numpy(norm_image1).float().to(self.device)
        if last_features:
            y_pred1, features = model(x_c_tensor1.unsqueeze(0),last_features)
        else:
            y_pred1 = model(x_c_tensor1.unsqueeze(0))
        y_hat1 = y_pred1.cpu().numpy()
        
        out[:] = y_hat1[0,1:]
          
        pred = np.rollaxis(out, 0, 3)
        
        print(pred.shape)
        if last_features: return pred,features
        else: return pred




