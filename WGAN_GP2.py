#coding: utf-8
import tensorflow as tf
from PIL import Image
from config import cfg
from utils import loadData
from resnet50 import Resnet50
from ops import *
import tensorflow.contrib.slim as slim

epsilon = 1e-9

class WGAN_GP(object):
    """
    setting1_9: 
    1. 使用pipeline读取训练图片
    2. G_enc为VGG2输出conv5(7x7x2048), G_dec全卷积网络(1x1卷积到512维,4个残差,连续反卷积+残差,用BN和ReLU
    3. D: 对应人脸先验知识有五个部分的判别器, 输入先进行减均值归一化, LayerNorm和LReLU, 全卷积网络(k4s2)最后全连接到1
    5. 判别器生成器用ADAM(0., 0.99), lr_G=lr_D=1e-4, 
    6. 两个网络的L2规则化
    7. critic = 1
    8. Lg:侧脸P通过enc和dec生成正脸P', 两者构成VGG余弦距离和对抗损失; 正脸F通过enc和dec生成正脸F', 两者构成VGG余弦距离\像素的L1损失\对抗损失
    9. Ld:对抗损失 \ 梯度惩罚
    10. 损失比 L1:fea:gan:gp = 0.01:250:1:10, 其中P:F=0.8:0.2
    11. 生成器网络结构变化
    12. casia侧脸和casia正脸预训练生成多样化正脸, 再缩小lr10倍casia侧脸和MPIE正脸生成归一化正脸
    modified:
    VGG距离改成欧式距离且: L1:fea:gan:gp = 0.01:0.01:1:10
    """
    def __init__(self):
        self.graph = tf.Graph()
        with self.graph.as_default():
            self.batch_size = cfg.batch_size
            self.data_feed = loadData(batch_size=self.batch_size, train_shuffle=True) # False
            
            # Construct Template Model (G_enc) to encoder input face
            with tf.variable_scope('face_model'):
                self.face_model = Resnet50() # Vgg16()
                self.face_model.build()
                print('VGG model built successfully.')
            
            # Construct G_dec and D
            if cfg.is_train:                
                self.is_train = tf.placeholder(tf.bool, name='is_train')
                self.profile, self.gt, self.front, self.resized_56, self.resized_112 = self.data_feed.get_train()
                
                # Construct Model
                self.build_arch()
                print('Model built successfully.')
                
                all_vars = tf.trainable_variables()
                self.vars_gen = [var for var in all_vars if var.name.startswith('decoder')]
                self.vars_dis = [var for var in all_vars if var.name.startswith('discriminator')]
                self.loss()
                               
                #################DEBUG#######################
                with tf.name_scope('Debug'):
                    grad1 = tf.gradients([self.feature_loss], [self.gen_p])[0]
                    self.grad1 = tf.reduce_mean(tf.sqrt(tf.reduce_sum(tf.square(grad1), [1,2,3])))
                    grad2 = tf.gradients([self.g_loss], [self.gen_p])[0]
                    self.grad2 = tf.reduce_mean(tf.sqrt(tf.reduce_sum(tf.square(grad2), [1,2,3])))
                    grad3 = tf.gradients([self.front_loss], [self.gen_f])[0]
                    self.grad3 = tf.reduce_mean(tf.sqrt(tf.reduce_sum(tf.square(grad3), [1,2,3])))
                # Summary
                self._summary()    
                
                # Trainer
                self.global_step = tf.Variable(0, name='global_step', trainable=False)
                self.train_gen = tf.train.AdamOptimizer(cfg.lr, beta1=cfg.beta1, beta2=cfg.beta2).minimize(
                                 self.gen_loss,
                                 global_step=self.global_step, var_list=self.vars_gen)
                self.train_dis = tf.train.AdamOptimizer(cfg.lr, beta1=cfg.beta1, beta2=cfg.beta2).minimize(
                                 self.dis_loss,
                                 global_step=self.global_step, var_list=self.vars_dis)
            else:
                self.profile = tf.placeholder("float", [None, 224, 224, 3], 'profile')
                self.is_train = tf.placeholder(tf.bool, name='is_train')
                self.front = tf.placeholder("float", [None, 224, 224, 3], 'front')
                
                self.build_arch()
                
        tf.logging.info('Seting up the main structure')

    def build_arch(self):
        # Use pretrained model(vgg-face) as encoder of Generator
        self.feature_p = self.face_model.forward(self.profile,'profile_enc')
        self.feature_f = self.face_model.forward(self.front, 'front_enc')
        print 'Face model output feature shape:', self.feature_p[3].get_shape()
        
        # Decoder front face from vgg feature
        self.gen_p = self.decoder(self.feature_p)
        self.gen_f = self.decoder(self.feature_f, reuse=True)
        print 'Generator output shape:', self.gen_p.get_shape()
        
        # Map texture into features again by VGG    
        _,_,_, self.pool5_gen_p = self.face_model.forward(self.gen_p,'profile_gen_enc')
        _,_,_, self.pool5_gen_f = self.face_model.forward(self.gen_f, 'front_gen_enc')
        print 'Feature of Generated Image shape:', self.pool5_gen_p.get_shape()
        
        # Construct discriminator between generalized front face and ground truth
        self.dr = self.discriminator(self.front)
        self.df1 = self.discriminator(self.gen_p, reuse=True)
        self.df2 = self.discriminator(self.gen_f, reuse=True)
        
        # Gradient Penalty #
        with tf.name_scope('gp'):
            alpha = tf.random_uniform((tf.shape(self.gen_p)[0], 1, 1, 1),minval = 0., maxval = 1.,)
            inter = self.front + alpha * (self.gen_p - self.front)
            d = self.discriminator(inter, reuse=True)
            grad = tf.gradients([d], [inter])[0]
            slopes = tf.sqrt(tf.reduce_sum(tf.square(grad), [1,2,3]))
            self.gradient_penalty = tf.reduce_mean(tf.square(slopes - 1.))
            ######
            self.grad4 = tf.reduce_mean(slopes)
                
    def decoder(self, feature, reuse=False):
        """
        decoder of generator, decoder feature from vgg
        args: 
            feature: face identity feature from VGG-16 / VGG-res50.
            reuse: Whether to reuse the model(Default False).
        return: 
            generated front face in [0, 255].
        """
        # The feature vector extracted from profile by VGG-16 is 4096-D
        # The feature vector extracted from profile by Resnet-50 is 2048-D
        with tf.variable_scope('decoder', reuse=reuse) as scope:        
            # Stacked Transpose Convolutions:(2048)
            #bn1 = batch_norm(name='bn1')
            norm = bn if(cfg.norm=='bn') else pixel_norm
            
            feat28,feat14,feat7,pool5 = feature[0],feature[1],feature[2],feature[3]
            
            if 0:   
                g_input = tf.reshape(fullyConnect(pool5, 4*4*512, 'fc0'), [-1, 4, 4, 512])
                #ouput shape: [4, 4, 512]
                with tf.variable_scope('dconv1'):
                    dconv1 = tf.nn.relu(norm(deconv2d(g_input, 256, 'dconv1', 
                                            kernel_size=4, strides = 1, padding='valid'),self.is_train,'norm1'))
                res1 = res_block(dconv1, 'res1', self.is_train, cfg.norm)
            
            with tf.variable_scope('conv0'):
                feat7 = tf.nn.relu(conv2d(feat7, 512, 'conv1', kernel_size=1, strides = 1))
                #feat7_1 = tf.nn.relu(conv2d(feat7, 256, 'conv2', kernel_size=1, strides = 1))
                #feat7_1_mirror = tf.reverse(feat7_1, axis=[2])
                #conv0 = tf.concat((feat7_0, feat7_1_mirror), axis=3)
            #ouput shape: [7, 7, 512]
            res1_0 = res_block(feat7, 'res1_0',self.is_train, cfg.norm)
            res1_1 = res_block(res1_0, 'res1_1',self.is_train, cfg.norm)
            res1_2 = res_block(res1_1, 'res1_2',self.is_train, cfg.norm)
            res1_3 = res_block(res1_2, 'res1_3',self.is_train, cfg.norm)
            #ouput shape: [7, 7, 512]
            with tf.variable_scope('dconv2'):
                #feat7 = tf.nn.relu(norm(conv2d(feat7, 256, 'feat7', kernel_size=1),self.is_train,'norm2_1'))
                dconv2 = tf.nn.relu(norm(deconv2d(res1_3, 256, 'dconv2', 
                                        kernel_size=4, strides = 2),self.is_train,'norm2_2'))
            res2 = res_block(dconv2, 'res2',self.is_train, cfg.norm)
            #ouput shape: [14, 14, 256]
            with tf.variable_scope('dconv3'):
                #feat14 = tf.nn.relu(norm(conv2d(feat14, 128, 'feat14', kernel_size=1),self.is_train,'norm3_1'))
                dconv3 = tf.nn.relu(norm(deconv2d(res2, 128, 'dconv2', 
                                        kernel_size=4, strides = 2),self.is_train,'norm3_2'))
            res3 = res_block(dconv3, 'res3',self.is_train, cfg.norm)
            #output shape: [28, 28, 128]
            with tf.variable_scope('dconv4'):
                #feat28 = tf.nn.relu(norm(conv2d(feat28, 64, 'feat28', kernel_size=1),self.is_train,'norm4_1'))
                dconv4 = tf.nn.relu(norm(deconv2d(res3, 64, 'dconv4', 
                                        kernel_size=4, strides = 2),self.is_train,'norm4_2'))
            res4 = res_block(dconv4, 'res4',self.is_train, cfg.norm)
            #output shape: [56, 56, 64]
            with tf.variable_scope('dconv5'):
                dconv5 = tf.nn.relu(norm(deconv2d(res4, 32, 'dconv5', kernel_size=4, strides = 2),self.is_train,'norm5'))
            res5 = res_block(dconv5, 'res5',self.is_train, cfg.norm)
            #input shape: [112, 112, 32]
            with tf.variable_scope('dconv6'):
                dconv6 = tf.nn.relu(norm(deconv2d(res5, 32, 'dconv6', kernel_size=4, strides = 2),self.is_train,'norm6'))
            res6 = res_block(dconv6, 'res6',self.is_train, cfg.norm)
            #output shape: [224, 224, 32]
            with tf.variable_scope('cw_conv'):
                gen = tf.nn.tanh(conv2d(res6, 3, 'pw_conv', kernel_size=1, strides = 1))
        
            return (gen + 1) * 127.5
        
    def discriminator(self, images, reuse=False):
        """
        Waasertein Distance, logits shape [bs, 1]
        args: 
            image: front face in [0,255]. [224,224,3]
            reuse: Whether to reuse the model(Default False).
        return: 
            a set of and logits.
        """
        with tf.variable_scope("discriminator", reuse=reuse) as scope:
            norm = slim.layer_norm
            
            images = images / 127.5 - 1
            eyes = tf.slice(images, [0,64,50,0], [self.batch_size,36,124,3]) #[64:100,50:174,:]
            nose = tf.slice(images, [0,75,90,0], [self.batch_size,65,44,3]) #[75:140,90:134,:]
            mouth = tf.slice(images, [0,140,75,0], [self.batch_size,30,74,3]) #[140:170,75:149,:]
            face = tf.slice(images, [0,64,50,0], [self.batch_size,116,124,3]) #[64:180,50:174,:]
            with tf.variable_scope("images"):
                with tf.variable_scope('d_conv0'):
                    h0_0 = lrelu(conv2d(images, 32, 'd_conv0', kernel_size=4, strides=2))
                # h0 is (112 x 112 x 32)
                with tf.variable_scope('d_conv1'):
                    h0_1 = lrelu(norm(conv2d(h0_0, 64, 'd_conv1', kernel_size=4, strides=2)))
                # h1 is (56 x 56 x 64)
                with tf.variable_scope('d_conv2'):
                    h0_2 = lrelu(norm(conv2d(h0_1, 128, 'd_conv2', kernel_size=4, strides=2)))
                # h2 is (28 x 28 x 128)
                with tf.variable_scope('d_conv3'):
                    h0_3 = lrelu(norm(conv2d(h0_2, 256, 'd_conv3', kernel_size=4, strides=2)))
                # h3 is (14 x 14 x 256)
                with tf.variable_scope('d_conv4'):
                    h0_4 = lrelu(norm(conv2d(h0_3, 256, 'd_conv4', kernel_size=4, strides=2)))
                # h4 is (7 x 7 x 256)
                with tf.variable_scope('d_fc'):
                    h0_4 = tf.reshape(h0_4, [self.batch_size, -1])
                    h0_5 = fullyConnect(h0_4, 1, 'd_fc')
                # h5 is (1)
            with tf.variable_scope("eyes"):
                with tf.variable_scope('d_conv0'):
                    h1_0 = lrelu(conv2d(eyes, 32, 'd_conv0', kernel_size=4, strides=2))
                # h0 is (18 x 62 x 32)
                with tf.variable_scope('d_conv1'):
                    h1_1 = lrelu(norm(conv2d(h1_0, 64, 'd_conv1', kernel_size=4, strides=2)))
                # h1 is (9 x 31 x 64)
                with tf.variable_scope('d_conv2'):
                    h1_2 = lrelu(norm(conv2d(h1_1, 128, 'd_conv2', kernel_size=4, strides=2)))
                # h2 is (5 x 15 x 128)
                with tf.variable_scope('d_conv3'):
                    h1_3 = lrelu(norm(conv2d(h1_2, 256, 'd_conv3', kernel_size=4, strides=2)))
                # h3 is (3 x 8 x 256)
                with tf.variable_scope('d_fc'):
                    h1_3 = tf.reshape(h1_3, [self.batch_size, -1])
                    h1_4 = fullyConnect(h1_3, 1, 'd_fc')
                # h4 is (1)
            with tf.variable_scope("nose"):
                with tf.variable_scope('d_conv0'):
                    h2_0 = lrelu(conv2d(nose, 32, 'd_conv0', kernel_size=4, strides=2))
                # h0 is (33 x 22 x 32)
                with tf.variable_scope('d_conv1'):
                    h2_1 = lrelu(norm(conv2d(h2_0, 64, 'd_conv1', kernel_size=4, strides=2)))
                # h1 is (17 x 11 x 64)
                with tf.variable_scope('d_conv2'):
                    h2_2 = lrelu(norm(conv2d(h2_1, 128, 'd_conv2', kernel_size=4, strides=2)))
                # h2 is (9 x 6 x 128)
                with tf.variable_scope('d_conv3'):
                    h2_3 = lrelu(norm(conv2d(h2_2, 256, 'd_conv3', kernel_size=4, strides=2)))
                # h3 is (5 x 3 x 256)
                with tf.variable_scope('d_fc'):
                    h2_3 = tf.reshape(h2_3, [self.batch_size, -1])
                    h2_4 = fullyConnect(h2_3, 1, 'd_fc')
                # h4 is (1)
            with tf.variable_scope("mouth"):
                with tf.variable_scope('d_conv0'):
                    h3_0 = lrelu(conv2d(mouth, 32, 'd_conv0', kernel_size=4, strides=2))
                # h0 is (15 x 37 x 32)
                with tf.variable_scope('d_conv1'):
                    h3_1 = lrelu(norm(conv2d(h3_0, 64, 'd_conv1', kernel_size=4, strides=2)))
                # h1 is (8 x 19 x 64)
                with tf.variable_scope('d_conv2'):
                    h3_2 = lrelu(norm(conv2d(h3_1, 128, 'd_conv2', kernel_size=4, strides=2)))
                # h2 is (4 x 10 x 128)
                with tf.variable_scope('d_conv3'):
                    h3_3 = lrelu(norm(conv2d(h3_2, 256, 'd_conv3', kernel_size=4, strides=2)))
                # h3 is (2 x 5 x 256)
                with tf.variable_scope('d_fc'):
                    h3_3 = tf.reshape(h3_3, [self.batch_size, -1])
                    h3_4 = fullyConnect(h3_3, 1, 'd_fc')
                # h4 is (1)
            with tf.variable_scope("face"):
                with tf.variable_scope('d_conv0'):
                    h4_0 = lrelu(conv2d(face, 32, 'd_conv0', kernel_size=4, strides=2))
                # h0 is (58 x 62 x 32)
                with tf.variable_scope('d_conv1'):
                    h4_1 = lrelu(norm(conv2d(h4_0, 64, 'd_conv1', kernel_size=4, strides=2)))
                # h1 is (29 x 31 x 64)
                with tf.variable_scope('d_conv2'):
                    h4_2 = lrelu(norm(conv2d(h4_1, 128, 'd_conv2', kernel_size=4, strides=2)))
                # h2 is (15 x 16 x 128)
                with tf.variable_scope('d_conv3'):
                    h4_3 = lrelu(norm(conv2d(h4_2, 256, 'd_conv3', kernel_size=4, strides=2)))
                # h3 is (8 x 8 x 256)
                with tf.variable_scope('d_fc'):
                    h4_3 = tf.reshape(h4_3, [self.batch_size, -1])
                    h4_4 = fullyConnect(h4_3, 1, 'd_fc')
                # h4 is (1)
            
            return h0_5, h1_4, h2_4, h3_4, h4_4

    def loss(self):
        """
        Loss Functions
        """
        with tf.name_scope('loss') as scope:
            with tf.name_scope('FeatureNorm'):
                pool5_p_norm = self.feature_p[3] / (tf.norm(self.feature_p[3], axis=1,keep_dims=True) + epsilon)
                pool5_f_norm = self.feature_f[3] / (tf.norm(self.feature_f[3], axis=1,keep_dims=True) + epsilon)
                pool5_gen_p_norm = self.pool5_gen_p / (tf.norm(self.pool5_gen_p, axis=1,keep_dims=True) + epsilon)
                pool5_gen_f_norm = self.pool5_gen_f / (tf.norm(self.pool5_gen_f, axis=1,keep_dims=True) + epsilon)
                        
            # 1. Frontalization Loss: L1-Norm
            self.front_loss = tf.reduce_mean(tf.reduce_sum(tf.abs(self.front/255. - self.gen_f/255.), [1,2,3]))
            if 0:
              with tf.name_scope('Pixel_Loss'):
                  #face_mask = Image.open('tt.bmp').crop([13,13,237,237])
                  #face_mask = np.array(face_mask, dtype=np.float32).reshape(224,224,1) / 255.0
                  #face_mask = np.tile(face_mask, [cfg.batch_size, 1, 1, 3])
                  #self.front_loss = tf.losses.absolute_difference(labels=self.front, 
                  #                                                predictions=self.texture)
                  #self.front_loss = tf.reduce_sum(tf.abs(self.gt/255. - self.texture/255.))
                  self.weights = tf.reduce_sum(tf.multiply(enc_fea_norm, enc_fea_gt_norm), [1])
                  abs_sum = tf.reduce_sum(tf.abs(self.gt/255. - self.gen_p/255.), [1,2,3])
                  self.front_loss = tf.reduce_mean(tf.multiply(self.weights, abs_sum))
                  tf.add_to_collection('losses', self.front_loss)
          
            # 2. Feature Loss: Cosine-Norm / L2-Norm
            with tf.name_scope('Perceptual_Loss'):
                #feature_loss = (1-cfg.w_f)*(1 - tf.reduce_sum(tf.multiply(pool5_p_norm, pool5_gen_p_norm), [1])) + \
                #               cfg.w_f*(1 - tf.reduce_sum(tf.multiply(pool5_f_norm, pool5_gen_f_norm), [1]))
                feature_loss = (1-cfg.w_f)*tf.reduce_sum(tf.square(self.feature_p[3] - self.pool5_gen_p), [1]) + \
                               cfg.w_f*tf.reduce_sum(tf.square(self.feature_f[3] - self.pool5_gen_f), [1])
                self.feature_loss = tf.reduce_mean(feature_loss) #/ 2 
                tf.add_to_collection('losses', self.feature_loss)
            
            # 3. L2 Regulation Loss
            with tf.name_scope('Regularation_Loss'):
                self.reg_gen = tf.contrib.layers.apply_regularization(
                    tf.contrib.layers.l2_regularizer(cfg.lambda_reg),
                    weights_list=[var for var in self.vars_gen if 'kernel' in var.name]
                )
                tf.add_to_collection('losses', self.reg_gen)
                self.reg_dis = tf.contrib.layers.apply_regularization(
                    tf.contrib.layers.l2_regularizer(cfg.lambda_reg),
                    weights_list=[var for var in self.vars_dis if 'kernel' in var.name]
                )
                tf.add_to_collection('losses', self.reg_dis)
            
            # 4. Adversarial Loss
            with tf.name_scope('Adversarial_Loss'):
                if 0: #MODE == 'lsgan':
                    self.g_loss = tf.reduce_mean((self.df1 - 1)**2)
                    self.d_loss = (tf.reduce_mean((self.dr - 1)**2) + tf.reduce_mean((self.df1 - 0)**2))/2.
                self.d_loss = tf.reduce_mean(tf.add_n(self.df1)*(1-cfg.w_f) + tf.add_n(self.df2)*cfg.w_f - tf.add_n(self.dr)) / 5
                self.g_loss = - tf.reduce_mean(tf.add_n(self.df1)*(1-cfg.w_f) + tf.add_n(self.df2)*cfg.w_f) / 5
                tf.add_to_collection('losses', self.d_loss)
                tf.add_to_collection('losses', self.g_loss)
            
            # 5. Symmetric Loss
            with tf.name_scope('Symmetric_Loss'):
                mirror_p = tf.reverse(self.gen_p, axis=[2])
                self.sym_loss = tf.reduce_mean(tf.reduce_sum(tf.abs(mirror_p/225. - self.gen_p/255.), [1,2,3]))
            
            # 6. Drift Loss
            with tf.name_scope('Drift_Loss'):
                self.drift_loss = 0
                #tf.reduce_mean(tf.add_n(tf.square(self.df)) + tf.add_n(tf.square(self.dr))) / 10
            
            # 7. Total Loss
            with tf.name_scope('Total_Loss'):  #
                self.gen_loss = cfg.lambda_l1 * self.front_loss + cfg.lambda_fea * self.feature_loss + \
                                cfg.lambda_gan * self.g_loss + self.reg_gen
                self.dis_loss = cfg.lambda_gan * self.d_loss + cfg.lambda_gp * self.gradient_penalty + \
                                self.reg_dis
                
    def _summary(self):
        """
        Tensorflow Summary
        """
        train_summary = []
        train_summary.append(tf.summary.scalar('train/d_loss', self.d_loss))
        train_summary.append(tf.summary.scalar('train/g_loss', self.g_loss))
        train_summary.append(tf.summary.scalar('train/gp', self.grad4))
        train_summary.append(tf.summary.scalar('train/feature_loss', self.feature_loss))
        train_summary.append(tf.summary.scalar('train/grad_feature', self.grad1))
        train_summary.append(tf.summary.scalar('train/grad_D', self.grad2))
        self.train_summary = tf.summary.merge(train_summary)
        
        #correct_prediction = tf.equal(tf.to_int32(self.labels), self.argmax_idx)
        #self.batch_accuracy = tf.reduce_sum(tf.cast(correct_prediction, tf.float32))
        #self.test_acc = tf.placeholder_with_default(tf.constant(0.), shape=[])
        
if '__name__' == '__main__':
    net = WGAN_GP()
    net.build()
