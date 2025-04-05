import torch
from NeuMF import NeuMF
import os
from data_load_perpos import simple_load_data_rate, get_model_data
from evaluation import model_evaluation_metric
from collections import defaultdict
import pandas as pd
from torch.optim.lr_scheduler import ReduceLROnPlateau
import torch.optim as optim
import torch.nn as nn

layer = [128, 64]
predictive_factor = 64

base_dir = os.getcwd()
name_rating_dir = "ratings.dat"
rating_data_file = os.path.join(base_dir, name_rating_dir)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

#train_dict, val_dict, test_dict, non_interacted_movies, movie_num, user_num = load_data_rate(file_name)

train_dict, valid_dict, test_dict, movie_num, user_num, removed_users_info= simple_load_data_rate(rating_data_file, negative_sample_no_train=5, negative_sample_no_valid=100, threshold=3)

train_user_input, train_movie_input, train_labels = get_model_data(train_dict)
valid_user_input, valid_movie_input, valid_labels = get_model_data(valid_dict)
test_user_input, test_movie_input, test_labels = get_model_data(test_dict)

gmf_model = torch.load('./5_gmf.pth')  # GMF
mlp_model = torch.load('./mlp_best.pth')  # MLP 

ncf_model = NeuMF(
    num_users=user_num + 1,  # +1 for 0-based indexing
    num_items=movie_num + 1,
    mf_dim=predictive_factor,       # Larger embeddings (vs. 10) for richer latent features
    layers=layer, # Deeper MLP for nonlinear interactions
            
).to(device)

ncf_model.gmf_user_embedding.weight.data.copy_(gmf_model.gmf_user_embedding.weight.data)
ncf_model.gmf_item_embedding.weight.data.copy_(gmf_model.gmf_item_embedding.weight.data)

ncf_model.mlp_user_embedding.weight.data.copy_(mlp_model.mlp_user_embedding.weight.data)
ncf_model.mlp_item_embedding.weight.data.copy_(mlp_model.mlp_item_embedding.weight.data)

for i in range(1, len(layer)):  
    ncf_model.mlp_layers[i].weight.data.copy_(mlp_model.mlp_layers[i].weight.data)
    ncf_model.mlp_layers[i].bias.data.copy_(mlp_model.mlp_layers[i].bias.data)

ncf_model.prediction.weight.data.copy_(0.5 * gmf_model.prediction.weight.data + 0.5 * mlp_model.prediction.weight.data)
ncf_model.prediction.bias.data.copy_(0.5 * gmf_model.prediction.bias.data + 0.5 * mlp_model.prediction.bias.data)

metrics = defaultdict(list)

# test before training
with torch.no_grad():
    test_recall, test_ndcg = model_evaluation_metric(ncf_model, test_dict, device, K=10)
    metrics['test_recall@10'] = [test_recall] * len(metrics['epoch'])
    metrics['test_ndcg@10'] = [test_ndcg] * len(metrics['epoch'])
    df_metrics = pd.DataFrame(metrics)
    df_metrics.to_csv(f'./pre_withouttrain_test.csv', index=False)
    print(f"Test Results for pre_withouttrain: Recall@10: {test_recall:.4f}, NDCG@10: {test_ndcg:.4f}")



# training
train_losses_ncf = []
val_losses_ncf = []
recalls_ncf = []
ndcgs_ncf = []
patience = 20
counter = 0
best_val_loss = float('inf')

results = {}
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

batch_size = 32
num_epochs = 50
model_ncf = ncf_model

optimizer = optim.Adam(
    model_ncf.parameters(),
    lr=0.001,       # Higher initial LR for faster convergence (MovieLens is dense)
     # Default momentum terms
    weight_decay=1e-5,  # Mild L2 regularization to prevent overfitting
    betas=(0.9, 0.999)
)
scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.2, patience=5, verbose=True)


criterion = nn.BCEWithLogitsLoss()
scaler = torch.cuda.amp.GradScaler()

user_input = torch.tensor(train_user_input, dtype=torch.long).to(device)
movie_input = torch.tensor(train_movie_input, dtype=torch.long).to(device)
train_labels = torch.tensor(train_labels, dtype=torch.float32).to(device)

dataset = torch.utils.data.TensorDataset(user_input, movie_input, train_labels)
dataloader = torch.utils.data.DataLoader(dataset, batch_size=batch_size)


val_user_input = torch.tensor(valid_user_input, dtype=torch.long).to(device)
val_movie_input = torch.tensor(valid_movie_input, dtype=torch.long).to(device)
val_labels = torch.tensor(valid_labels, dtype=torch.float32).to(device)

val_dataset = torch.utils.data.TensorDataset(val_user_input, val_movie_input, val_labels)
val_dataloader = torch.utils.data.DataLoader(val_dataset, batch_size=batch_size)
model_ncf = torch.compile(model_ncf)  # Optimize model (PyTorch 2.0+)
model_ncf = torch.nn.DataParallel(model_ncf)  # Enable Multi-GPU

metrics = defaultdict(list)  # Tracks all metrics across epochs

for epoch in range(num_epochs):
    model_ncf.train()
    total_loss = 0

    for batch_users, batch_items, batch_labels in dataloader:
        batch_users = batch_users.to(device)
        batch_items = batch_items.to(device)
        batch_labels = batch_labels.to(device)
        optimizer.zero_grad(set_to_none=True)
        with torch.amp.autocast('cuda'):
            predictions = model_ncf(batch_users, batch_items)
            loss = criterion(predictions, batch_labels.view(-1, 1))
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        total_loss += loss.item()

    print(f"Epoch {epoch + 1}, Training Loss: {total_loss / len(dataloader)}")

    model_ncf.eval()
    val_loss = 0.0
   
    
    with torch.inference_mode():  # Best for inference
        with torch.cuda.amp.autocast():  # Mixed precision
            for batch_users, batch_items, batch_labels in val_dataloader:
                batch_users = batch_users.to(device, non_blocking=True)
                batch_items = batch_items.to(device, non_blocking=True)
                batch_labels = batch_labels.to(device, non_blocking=True)
    
                predictions = model_ncf(batch_users, batch_items)  # Optimized inference

   
                loss = criterion(predictions, batch_labels.view(-1, 1))
                val_loss += loss.item()
            val_loss_avg = val_loss / len(val_dataloader)
            scheduler.step(val_loss_avg) if len(val_dataloader) > 0 else 0.0
            print(f"Epoch {epoch + 1}, Validation Loss: {val_loss_avg}")
    
    
    with torch.no_grad():
        recall, ndcg = model_evaluation_metric(model_ncf, valid_dict, device, K=10)
        recalls_ncf.append(recall)
        ndcgs_ncf.append(ndcg)

        # Save training and validation metrics
        metrics['epoch'].append(epoch + 1)
        metrics['train_loss'].append(total_loss / len(dataloader))
        metrics['val_loss'].append(val_loss_avg)
        metrics['recall@10'].append(recall)
        metrics['ndcg@10'].append(ndcg)
        metrics['lr'].append(optimizer.param_groups[0]['lr'])  # Log learning rate
        
        # Save to CSV every epoch
        df_metrics = pd.DataFrame(metrics)
        df_metrics.to_csv(f'./after_train.csv', index=False)

        # Early stopping mechanism
        tolerance = 0.001  # Allow minor fluctuations
        if val_loss_avg < (best_val_loss - tolerance):
            best_val_loss = val_loss_avg
            counter = 0
            best_model = model_ncf
        else:
            counter += 1
            print(f"Early Stopping Counter: {counter}/{patience}")
            if counter >= patience:
                print("Early stopping: Loss stagnated.")
                torch.save(best_model, f"./after_train.pth") 
                break


with torch.no_grad():
    test_recall, test_ndcg = model_evaluation_metric(best_model, test_dict, device, K=10)
    print(f"\n=== Test Recall@10: {test_recall:.4f}, Test NDCG@10: {test_ndcg:.4f} ===\n")
    
    # Add test results to the dictionary
    metrics['test_recall@10'] = [test_recall] * len(metrics['epoch'])  # Same value across all epochs
    metrics['test_ndcg@10'] = [test_ndcg] * len(metrics['epoch'])  # Same value across all epochs
    
    # Save the final metrics including test results to CSV
    df_metrics = pd.DataFrame(metrics)
    df_metrics.to_csv(f'./pre_aftertrain_test.csv', index=False)

    # Store results for reference
    results['name'] = {
        "test_recall": test_recall,
        "test_ndcg": test_ndcg
    }
    print(f"Test Results: {results['name']}")